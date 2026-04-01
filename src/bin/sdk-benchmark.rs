//! MCP Streamable HTTP Benchmark
//! Uses rmcp with Streamable HTTP transport
//!
//! Measures three phases:
//! - Init: Session initialization RPS and latency
//! - Tool/list: Tool listing RPM and latency
//! - Tool call: Tool call RPS and latency

use base64::Engine;
use clap::Parser;
use hmac::{Hmac, Mac};
use log::{debug, info};
use rand::Rng;
use rmcp::{model::*, ServiceExt};
use rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig;
use serde::Serialize;
use serde_json::Value;
use sha2::Sha256;
use std::env;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;
use tokio::task::JoinSet;

type HmacSha256 = Hmac<Sha256>;

/// JWT Header
#[derive(Serialize)]
struct JwtHeader {
    alg: String,
    typ: String,
}

/// JWT Claims
#[derive(Serialize)]
struct JwtClaims {
    sub: String,
    exp: i64,
    iat: i64,
    aud: String,
    iss: String,
    jti: String,
    token_use: String,
    user: JwtUser,
}

/// JWT User info
#[derive(Serialize)]
struct JwtUser {
    email: String,
    full_name: String,
    is_admin: bool,
    auth_provider: String,
}

/// Benchmark configuration
struct BenchmarkConfig {
    host: String,
    server_id: String,
    jwt_secret_key: String,
    jwt_algorithm: String,
    jwt_audience: String,
    jwt_issuer: String,
    jwt_username: String,
    bearer_token: Option<String>,
}

impl Default for BenchmarkConfig {
    fn default() -> Self {
        Self {
            host: env::var("K6_MCP_HOST").unwrap_or_else(|_| "http://localhost:8080".to_string()),
            server_id: env::var("K6_MCP_SERVER_ID").unwrap_or_default(),
            jwt_secret_key: env::var("K6_JWT_SECRET_KEY")
                .unwrap_or_else(|_| "my-test-key-but-now-longer-than-32-bytes".to_string()),
            jwt_algorithm: env::var("K6_JWT_ALGORITHM").unwrap_or_else(|_| "HS256".to_string()),
            jwt_audience: env::var("K6_JWT_AUDIENCE").unwrap_or_else(|_| "mcpgateway-api".to_string()),
            jwt_issuer: env::var("K6_JWT_ISSUER").unwrap_or_else(|_| "mcpgateway".to_string()),
            jwt_username: env::var("K6_JWT_USERNAME").unwrap_or_else(|_| "admin@example.com".to_string()),
            bearer_token: env::var("K6_BEARER_TOKEN").ok(),
        }
    }
}

/// Generate a JWT token matching gateway expectations
fn generate_jwt_token(config: &BenchmarkConfig) -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let exp = now + 8760 * 3600; // 1 year

    // Generate random JTI
    let mut rng = rand::rng();
    let jti_bytes: [u8; 16] = rng.random();
    let jti: String = jti_bytes.iter().map(|b| format!("{:02x}", b)).collect();

    let header = JwtHeader {
        alg: config.jwt_algorithm.clone(),
        typ: "JWT".to_string(),
    };

    let claims = JwtClaims {
        sub: config.jwt_username.clone(),
        exp,
        iat: now,
        aud: config.jwt_audience.clone(),
        iss: config.jwt_issuer.clone(),
        jti,
        token_use: "session".to_string(),
        user: JwtUser {
            email: config.jwt_username.clone(),
            full_name: "Rust MCP Benchmark".to_string(),
            is_admin: true,
            auth_provider: "local".to_string(),
        },
    };

    // Serialize and encode header
    let header_json = serde_json::to_string(&header).unwrap();
    let header_encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(header_json.as_bytes());

    // Serialize and encode payload
    let claims_json = serde_json::to_string(&claims).unwrap();
    let claims_encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(claims_json.as_bytes());

    // Create signature
    let message = format!("{}.{}", header_encoded, claims_encoded);
    let mut mac = HmacSha256::new_from_slice(config.jwt_secret_key.as_bytes()).unwrap();
    mac.update(message.as_bytes());
    let signature = mac.finalize().into_bytes();
    let signature_encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&signature);

    format!("{}.{}.{}", header_encoded, claims_encoded, signature_encoded)
}

/// Get or generate JWT token (cached for benchmark duration)
fn get_bearer_token(config: &BenchmarkConfig) -> String {
    if let Some(token) = &config.bearer_token {
        info!("Using pre-configured bearer token");
        return token.clone();
    }
    let token = generate_jwt_token(config);
    // Debug: print token parts (masked)
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() == 3 {
        let header_json = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(parts[0]).unwrap_or_default();
        let payload_json = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(parts[1]).unwrap_or_default();
        debug!("JWT Header: {}", String::from_utf8_lossy(&header_json));
        debug!("JWT Payload: {}", String::from_utf8_lossy(&payload_json));
        debug!("JWT Signature: {}...", &parts[2][..8.min(parts[2].len())]);
    }
    debug!("JWT Token generated: {}...", &token[..20.min(token.len())]);
    token
}

#[derive(Parser, Debug)]
#[command(name = "sdk-benchmark")]
#[command(about = "MCP Streamable HTTP Benchmark")]
struct Args {
    /// Server URL
    #[arg(short = 's', default_value = "http://localhost:8000/mcp")]
    server_url: String,

    /// Number of runs per user for tool calls
    #[arg(short = 'r', default_value = "100")]
    runs: usize,

    /// Number of runs for init and list (default: same as runs)
    #[arg(short = 'i', default_value = None)]
    init_runs: Option<usize>,

    /// Tool name to call for benchmark
    #[arg(short = 't', default_value = "say_hello")]
    tool_name: String,

    /// JSON arguments to pass to the tool
    #[arg(short = 'a', default_value = "{}")]
    arguments: String,

    /// Number of concurrent clients
    #[arg(short = 'u', default_value = "1")]
    users: usize,

    /// Enable JWT token generation for authentication
    #[arg(long = "with-jwt", default_value = "false")]
    with_jwt: bool,
}

struct BenchmarkStats {
    total_requests: usize,
    successful_requests: usize,
    failed_requests: usize,
    total_latency: Duration,
}

impl BenchmarkStats {
    fn new() -> Self {
        Self {
            total_requests: 0,
            successful_requests: 0,
            failed_requests: 0,
            total_latency: Duration::ZERO,
        }
    }
}

struct PhaseStats {
    name: &'static str,
    total_requests: usize,
    successful_requests: usize,
    failed_requests: usize,
    total_latency: Duration,
    elapsed: Duration,
}

impl Clone for PhaseStats {
    fn clone(&self) -> Self {
        Self {
            name: self.name,
            total_requests: self.total_requests,
            successful_requests: self.successful_requests,
            failed_requests: self.failed_requests,
            total_latency: self.total_latency,
            elapsed: self.elapsed,
        }
    }
}

impl PhaseStats {
    fn new(name: &'static str) -> Self {
        Self {
            name,
            total_requests: 0,
            successful_requests: 0,
            failed_requests: 0,
            total_latency: Duration::ZERO,
            elapsed: Duration::ZERO,
        }
    }

    fn throughput(&self) -> f64 {
        if self.elapsed.as_secs_f64() > 0.0 {
            self.successful_requests as f64 / self.elapsed.as_secs_f64()
        } else {
            0.0
        }
    }

    fn avg_latency_ms(&self) -> f64 {
        if self.successful_requests > 0 {
            self.total_latency.as_secs_f64() / self.successful_requests as f64 * 1000.0
        } else {
            0.0
        }
    }

    fn print_results(&self) {
        println!("\n📊 {} Results:", self.name);
        println!(
            "   Total: {} requests ({} success, {} failed)",
            self.total_requests, self.successful_requests, self.failed_requests
        );
        println!("   Elapsed: {:.2?}", self.elapsed);
        println!("   Avg latency: {:.2}ms", self.avg_latency_ms());
        println!("   Throughput: {:.2} req/s", self.throughput());
    }
}

async fn run_init_benchmark(
    server_url: &str,
    concurrent_clients: usize,
    runs_per_client: usize,
    bearer_token: Option<&str>,
) -> PhaseStats {
    let stats = Arc::new(Mutex::new(PhaseStats::new("Init")));
    let mut tasks = JoinSet::new();

    let total_requests = concurrent_clients * runs_per_client;
    println!(
        "\n🚀 Phase 1 - Init Benchmark: {} clients × {} runs = {} total requests",
        concurrent_clients, runs_per_client, total_requests
    );
    println!("   Server: {}", server_url);

    let start_time = Instant::now();

    for client_id in 0..concurrent_clients {
        let stats_clone = Arc::clone(&stats);
        let server_url = server_url.to_string();
        let bearer_token = bearer_token.map(|t| t.to_string());

        tasks.spawn(async move {
            let client_stats = benchmark_init(client_id, server_url, runs_per_client, bearer_token.as_deref()).await;

            let mut stats = stats_clone.lock().await;
            stats.total_requests += client_stats.total_requests;
            stats.successful_requests += client_stats.successful_requests;
            stats.failed_requests += client_stats.failed_requests;
            stats.total_latency += client_stats.total_latency;
        });
    }

    // Wait for all tasks to complete
    while let Some(result) = tasks.join_next().await {
        if let Err(e) = result {
            eprintln!("Task error: {}", e);
        }
    }

    let mut final_stats = stats.lock().await;
    final_stats.elapsed = start_time.elapsed();
    final_stats.clone()
}

async fn run_list_benchmark(
    server_url: &str,
    concurrent_clients: usize,
    runs_per_client: usize,
    bearer_token: Option<&str>,
) -> PhaseStats {
    let stats = Arc::new(Mutex::new(PhaseStats::new("Tool/list")));
    let mut tasks = JoinSet::new();

    let total_requests = concurrent_clients * runs_per_client;
    println!(
        "\n🚀 Phase 2 - Tool/list Benchmark: {} clients × {} runs = {} total requests",
        concurrent_clients, runs_per_client, total_requests
    );
    println!("   Server: {}", server_url);

    let start_time = Instant::now();

    for client_id in 0..concurrent_clients {
        let stats_clone = Arc::clone(&stats);
        let server_url = server_url.to_string();
        let bearer_token = bearer_token.map(|t| t.to_string());

        tasks.spawn(async move {
            let client_stats = benchmark_list_tools(client_id, server_url, runs_per_client, bearer_token.as_deref()).await;

            let mut stats = stats_clone.lock().await;
            stats.total_requests += client_stats.total_requests;
            stats.successful_requests += client_stats.successful_requests;
            stats.failed_requests += client_stats.failed_requests;
            stats.total_latency += client_stats.total_latency;
        });
    }

    // Wait for all tasks to complete
    while let Some(result) = tasks.join_next().await {
        if let Err(e) = result {
            eprintln!("Task error: {}", e);
        }
    }

    let mut final_stats = stats.lock().await;
    final_stats.elapsed = start_time.elapsed();
    final_stats.clone()
}

async fn run_tool_call_benchmark(
    server_url: &str,
    concurrent_clients: usize,
    runs_per_client: usize,
    tool_name: &str,
    arguments: Option<Value>,
    bearer_token: Option<&str>,
) -> PhaseStats {
    let stats = Arc::new(Mutex::new(PhaseStats::new("Tool call")));
    let mut tasks = JoinSet::new();

    let total_requests = concurrent_clients * runs_per_client;
    println!(
        "\n🚀 Phase 3 - Tool Call Benchmark: {} clients × {} runs = {} total requests",
        concurrent_clients, runs_per_client, total_requests
    );
    println!("   Server: {}", server_url);
    println!("   Tool: {}", tool_name);
    if let Some(ref args) = arguments {
        println!("   Arguments: {}", args);
    }

    let start_time = Instant::now();

    for client_id in 0..concurrent_clients {
        let stats_clone = Arc::clone(&stats);
        let server_url = server_url.to_string();
        let tool_name = tool_name.to_string();
        let arguments = arguments.clone();
        let bearer_token = bearer_token.map(|t| t.to_string());

        tasks.spawn(async move {
            let client_stats = benchmark_tool_call(
                client_id,
                server_url,
                runs_per_client,
                tool_name,
                arguments,
                bearer_token.as_deref(),
            )
            .await;

            let mut stats = stats_clone.lock().await;
            stats.total_requests += client_stats.total_requests;
            stats.successful_requests += client_stats.successful_requests;
            stats.failed_requests += client_stats.failed_requests;
            stats.total_latency += client_stats.total_latency;
        });
    }

    // Wait for all tasks to complete
    while let Some(result) = tasks.join_next().await {
        if let Err(e) = result {
            eprintln!("Task error: {}", e);
        }
    }

    let mut final_stats = stats.lock().await;
    final_stats.elapsed = start_time.elapsed();
    final_stats.clone()
}

async fn benchmark_init(
    client_id: usize,
    base_url: String,
    num_requests: usize,
    bearer_token: Option<&str>,
) -> BenchmarkStats {
    let mut stats = BenchmarkStats::new();

    for i in 0..num_requests {
        let start = Instant::now();

        let mut config = StreamableHttpClientTransportConfig::with_uri(base_url.as_str());
        if let Some(token) = bearer_token {
            config = config.auth_header(token.to_string());
        }
        let transport = rmcp::transport::StreamableHttpClientTransport::from_config(config);
        let handler = ();

        match handler.serve(transport).await {
            Ok(mut running_service) => {
                // Explicitly close the session and wait for cleanup
                if let Err(e) = running_service.close().await {
                    stats.failed_requests += 1;
                    if i == 0 {
                        eprintln!("   Client {} session close error: {}", client_id, e);
                    }
                    stats.total_requests += 1;
                    stats.total_latency += start.elapsed();
                    continue;
                }
                stats.successful_requests += 1;
            }
            Err(e) => {
                stats.failed_requests += 1;
                if i == 0 {
                    eprintln!("   Client {} init error: {}", client_id, e);
                }
            }
        }

        stats.total_requests += 1;
        stats.total_latency += start.elapsed();
    }

    stats
}

async fn benchmark_list_tools(
    client_id: usize,
    base_url: String,
    num_requests: usize,
    bearer_token: Option<&str>,
) -> BenchmarkStats {
    let mut stats = BenchmarkStats::new();

    let mut config = StreamableHttpClientTransportConfig::with_uri(base_url.as_str());
    if let Some(token) = bearer_token {
        config = config.auth_header(token.to_string());
    }
    let transport = rmcp::transport::StreamableHttpClientTransport::from_config(config);
    let handler = ();

    let client_peer = match handler.serve(transport).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("   Client {} failed to initialize: {}", client_id, e);
            stats.failed_requests = num_requests;
            stats.total_requests = num_requests;
            return stats;
        }
    };

    // Reuse session for all list_tools calls
    for i in 0..num_requests {
        let start = Instant::now();

        match client_peer.list_tools(None).await {
            Ok(_) => {
                stats.successful_requests += 1;
            }
            Err(e) => {
                stats.failed_requests += 1;
                if i == 0 {
                    eprintln!("   Client {} list_tools error: {}", client_id, e);
                }
            }
        }

        stats.total_requests += 1;
        stats.total_latency += start.elapsed();
    }

    stats
}

async fn benchmark_tool_call(
    client_id: usize,
    base_url: String,
    num_requests: usize,
    tool_name: String,
    arguments: Option<Value>,
    bearer_token: Option<&str>,
) -> BenchmarkStats {
    let mut stats = BenchmarkStats::new();

    let mut config = StreamableHttpClientTransportConfig::with_uri(base_url.as_str());
    if let Some(token) = bearer_token {
        config = config.auth_header(token.to_string());
    }
    let transport = rmcp::transport::StreamableHttpClientTransport::from_config(config);
    let handler = ();

    let client_peer = match handler.serve(transport).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("   Client {} failed to initialize: {}", client_id, e);
            stats.failed_requests = num_requests;
            stats.total_requests = num_requests;
            return stats;
        }
    };

    // Reuse session for all tool calls
    for i in 0..num_requests {
        let start = Instant::now();

        // Build tool call parameters using owned String
        let mut params = CallToolRequestParams::new(tool_name.to_owned());
        if let Some(ref args) = arguments {
            if let Value::Object(obj) = args {
                params = params.with_arguments(obj.clone());
            }
        }

        // Call specified tool
        match client_peer.call_tool(params).await {
            Ok(result) => {
                // Check if the tool call returned an error (is_error flag)
                if result.is_error.unwrap_or(false) {
                    stats.failed_requests += 1;
                    if i == 0 {
                        // Extract error message from content
                        let error_msg = result.content.iter().find_map(|c| {
                            if let RawContent::Text(text) = &**c {
                                Some(text.text.clone())
                            } else {
                                None
                            }
                        }).unwrap_or_else(|| "Tool call failed".to_string());
                        eprintln!("   Client {} tool error: {}", client_id, error_msg);
                    }
                } else if !result.content.is_empty() {
                    stats.successful_requests += 1;
                } else {
                    stats.failed_requests += 1;
                    if i == 0 {
                        eprintln!("   Client {} error: Empty response", client_id);
                    }
                }
            }
            Err(e) => {
                stats.failed_requests += 1;
                if i == 0 {
                    eprintln!("   Client {} error: {}", client_id, e);
                }
            }
        }

        stats.total_requests += 1;
        stats.total_latency += start.elapsed();
    }

    stats
}

/// Verify that the tool exists on the server, otherwise print available tools
async fn verify_tool_exists(
    base_url: &str,
    tool_name: &str,
    bearer_token: Option<&str>,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut config = StreamableHttpClientTransportConfig::with_uri(base_url);
    if let Some(token) = bearer_token {
        config = config.auth_header(token.to_string());
    }
    let transport = rmcp::transport::StreamableHttpClientTransport::from_config(config);
    let handler = ();

    let client_peer = handler
        .serve(transport)
        .await
        .map_err(|e| format!("Failed to connect to server: {}", e))?;

    // Try to list tools
    let tools_result = client_peer
        .list_tools(None)
        .await
        .map_err(|e| format!("Failed to list tools: {}", e))?;

    let tool_names: Vec<&str> = tools_result.tools.iter().map(|t| t.name.as_ref()).collect();

    if !tool_names.contains(&tool_name) {
        eprintln!("\n❌ Tool '{}' not found on server", tool_name);
        eprintln!("\nAvailable tools:");
        for name in tool_names {
            eprintln!("  - {}", name);
        }
        return Err(format!("Tool '{}' not found", tool_name).into());
    }

    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("info")
    ).init();

    let mut args = Args::parse();

    // Create benchmark config from environment variables
    let config = BenchmarkConfig::default();

    // Override runs from environment if set
    if let Ok(r) = env::var("RUNS") {
        if let Ok(n) = r.parse::<usize>() {
            if n > 0 {
                args.runs = n;
            }
        }
    }

    // Use server_url from args if provided, otherwise from config
    let server_url = if args.server_url.is_empty() {
        format!("{}/servers/{}/mcp", config.host, config.server_id)
    } else {
        args.server_url.clone()
    };

    let init_runs = args.init_runs.unwrap_or(args.runs);

    // Get or generate JWT token if --with-jwt flag is set
    let bearer_token = if args.with_jwt {
        Some(get_bearer_token(&config))
    } else {
        None
    };

    println!("🔌 MCP Streamable HTTP Benchmark");
    println!("   Transport: Streamable HTTP");
    println!("   Users: {}, Init runs: {}, Tool call runs: {}", args.users, init_runs, args.runs);
    println!("   Server: {}", server_url);
    if args.with_jwt {
        info!("JWT authentication enabled");
    }

    // Parse JSON arguments if provided
    let tool_arguments = if args.arguments.is_empty() {
        None
    } else {
        // Parse as Value
        let value: Value = serde_json::from_str(&args.arguments)
            .map_err(|e| format!("Failed to parse JSON arguments: {}", e))?;

        // Ensure it's an object
        match value {
            Value::Object(_) => Some(value),
            _ => {
                return Err(
                    "JSON arguments must be an object (e.g., {\"key\": \"value\"})".into(),
                )
            }
        }
    };

    // Verify tool exists on server
    println!("   Verifying tool '{}' exists...", args.tool_name);
    verify_tool_exists(&server_url, &args.tool_name, bearer_token.as_deref()).await?;
    println!("   ✅ Tool '{}' found", args.tool_name);

    // Phase 1: Init benchmark
    let init_stats = run_init_benchmark(&server_url, args.users, init_runs, bearer_token.as_deref()).await;
    init_stats.print_results();

    // Phase 2: Tool/list benchmark
    let list_stats = run_list_benchmark(&server_url, args.users, init_runs, bearer_token.as_deref()).await;
    list_stats.print_results();

    // Phase 3: Tool call benchmark
    let tool_call_stats = run_tool_call_benchmark(
        &server_url,
        args.users,
        args.runs,
        &args.tool_name,
        tool_arguments,
        bearer_token.as_deref(),
    )
    .await;
    tool_call_stats.print_results();

    // Summary
    println!("\n📈 Summary:");
    println!("   Init:      {:.2} req/s,  {:.2}ms avg latency", init_stats.throughput(), init_stats.avg_latency_ms());
    println!("   Tool/list: {:.2} req/s,  {:.2}ms avg latency", list_stats.throughput(), list_stats.avg_latency_ms());
    println!("   Tool call: {:.2} req/s,  {:.2}ms avg latency", tool_call_stats.throughput(), tool_call_stats.avg_latency_ms());

    Ok(())
}

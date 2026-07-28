//! Opt-in Rust Desktop probe for a live Go Relay + Python Coworker stack.
//!
//! Run with:
//! COWORKER_RELAY_BASE_URL=... COWORKER_RELAY_TOKEN=... \
//! cargo test -p coworker-desktop-core --test relay_live -- --ignored

use coworker_desktop_core::relay_transport;

#[tokio::test]
#[ignore = "requires a separately running Go Relay and paired Python Coworker"]
async fn desktop_reaches_coworker_only_through_e2ee_routes() {
    let base_url =
        std::env::var("COWORKER_RELAY_BASE_URL").expect("COWORKER_RELAY_BASE_URL is required");
    let token = std::env::var("COWORKER_RELAY_TOKEN").expect("COWORKER_RELAY_TOKEN is required");

    let status = relay_transport::request(
        &base_url,
        &token,
        "GET",
        "/status",
        vec![("accept".into(), "application/json".into())],
        vec![],
    )
    .await
    .expect("E2EE status request");
    assert_eq!(status.status, 200);

    let blocked = relay_transport::request(
        &base_url,
        &token,
        "GET",
        "/api/admin/config",
        vec![("accept".into(), "application/json".into())],
        vec![],
    )
    .await
    .expect("blocked route response");
    assert_eq!(blocked.status, 404);
}

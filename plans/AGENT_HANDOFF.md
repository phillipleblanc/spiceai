# Agent Handoff: Verify Build and Complete Connector Extraction

## Context

We're extracting data connectors from the `runtime` crate into separate crates under `crates/data-connectors/` to enable faster incremental builds. 21 connectors have been extracted. The current session made lint fixes and cleanup but couldn't verify the build due to system resource constraints.

## Current State

- **Branch**: `phillip/260115-runtime-split`
- **Working commit**: `ynxumrnl` - "fix: resolve clippy lint errors in connector crates"
- **Status**: Changes staged but build verification blocked

## What Was Done This Session

1. Fixed clippy lint errors in 8 files (doc_markdown, missing_errors_doc, unused_import, useless_conversion, deprecated BehaviorVersion)
2. Added connector linkage in `bin/spiced/src/lib.rs` (20 `use connector_xxx as _;` statements)
3. Removed unused optional deps from `crates/runtime/Cargo.toml` (clickhouse-rs, scylla, snowflake-api, tiberius, mongodb, mysql_async, graph-rs-sdk, imap)

## Tasks to Complete

```bash
# 1. Verify the build compiles
make lint-rust 2>&1

# 2. If lint fails, fix any remaining issues

# 3. If lint passes, the commits are ready - optionally squash the lint fix into relevant connector commits or leave as-is

# 4. Check current commit state
jj log --limit 10
jj status
```

## Known Issues to Watch For

- `model_components` has a pre-existing `unused_variables` error (not our change) - may need `--features full` or a separate fix
- NFS connector is excluded from workspace (requires system libnfs library)
- The `objc_exception` crate fails on Linux with `--all-features` due to Objective-C compilation - use default features

## Files Modified (in working copy)

```
bin/spiced/src/lib.rs
crates/aws-sdk-credential-bridge/src/lib.rs
crates/data-connectors/connector-delta-lake/src/lib.rs
crates/data-connectors/connector-dremio/src/lib.rs
crates/data-connectors/connector-duckdb/src/lib.rs
crates/data-connectors/connector-flightsql/src/lib.rs
crates/data-connectors/connector-mongodb/src/lib.rs
crates/data-connectors/connector-oracle/src/lib.rs
crates/data-connectors/connector-scylladb/src/lib.rs
crates/runtime/Cargo.toml
```

## Success Criteria

- `make lint-rust` passes
- All 21 extracted connectors compile and register correctly via `linkme` distributed slices

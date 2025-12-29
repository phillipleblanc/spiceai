# Dynamic Object Store Datasets

## Goal

Allow queries like `SELECT * FROM my_table('s3://my-bucket/my-dataset')` by registering a dataset
stub that provides authentication and configuration, and treating its `from` as a prefix for
dynamic object-store paths.

## Deep Dive Findings

- Dataset definitions come from `crates/spicepod/src/component/dataset.rs` and are materialized
  in `crates/runtime/src/component/dataset/builder.rs`.
- Runtime dataset loading happens in `crates/runtime/src/init/dataset.rs`, which registers
  tables through `crates/runtime/src/datafusion/mod.rs`.
- Object-store datasets use `ListingTableConnector` (`crates/runtime/src/dataconnector/listing/connector.rs`).
  It builds `ListingTable` providers based on `dataset.from` and connector params, and
  relies on URL fragments for auth (`listing::build_fragments`).
- Object store credentials are wired via URL fragments in
  `crates/runtime-object-store/src/registry/mod.rs`.
- Current "dynamic" behavior is limited to deferred connectors and HTTP non-structured datasets
  (`crates/runtime/src/dataconnector/deferred.rs`, `crates/runtime/src/dataconnector/https.rs`),
  and still depends on a fixed dataset name.
- SQL parsing utilities already exist for extracting table references in
  `crates/runtime/src/view.rs`, but there is no path-based table resolution today.

## Plan

1. Spicepod and runtime model
   - Use dataset param flag `params.dynamic = true` (params already exist; no new dataset field).
     Only runtime needs to read and interpret it.
   - Extend runtime dataset structs (`crates/runtime/src/component/dataset/*`) to store the
     dynamic flag and validate restrictions (read-only, no acceleration, no embeddings).
   - Scope to object-store listing connectors only (S3/ABFS/GCS/file/HTTP listing); reject other
     connectors in validation with a clear error.
2. Dynamic dataset wiring
   - For datasets with `params.dynamic = true`, register a UDTF named after the dataset and
     skip table registration in DataFusion.
   - Store the dataset stub + connector inside the UDTF for prefix enforcement and provider
     creation.
3. Query preprocessing and table detection
   - No SQL rewrite; require explicit UDTF usage in queries.
4. Dynamic table provider creation
   - Resolve the UDTF argument to a full URL (relative paths resolve against the dataset prefix).
   - Validate containment using `ListingTableUrl::contains` semantics.
   - Clone the stub dataset and override `from` with the requested path.
   - Build the provider using the existing connector (`read_provider`) or a new
     `ListingTableConnector::read_provider_with_url` helper to avoid mutating the stub.
   - Require `file_format` when the path is a directory and has no extension; keep extension
     inference for single files.
5. Registration and caching strategy
   - Register a UDTF per dynamic dataset using the dataset name (e.g., `my_table(...)`).
   - The UDTF returns a `TableProvider` without global registration.
   - The UDTF accepts either:
     - relative paths (resolved against the dataset `from` prefix), or
     - absolute URLs (must match the same prefix).
6. Security and error handling
   - Enforce prefix-only access; reject cross-bucket or prefix escapes.
   - Consider allowlist integration so dynamic access respects existing table policies.
   - Emit single-line errors; avoid disclosing presence of forbidden paths.
7. Tests and docs
   - Add runtime tests (likely in `crates/runtime/tests/s3` or a new module) that cover:
     - Single-file reads under a dynamic prefix.
     - Hive directory reads under a dynamic prefix.
     - Access denied for paths outside the prefix.
   - Update docs to describe syntax, required params, and limitations (this document plus
     reference docs as needed).

## UDTF Signature

Dynamic datasets register a table function with the same name as the dataset.

```
my_table(path)
```

Arguments:
- `path` (string, required): Either a relative path or an absolute URL.

Resolution rules:
- Relative paths are resolved against the dataset `from` prefix.
- Absolute URLs must match the same prefix and object store settings.
- Prefix checks use `ListingTableUrl::contains` to prevent cross-bucket or prefix escapes.

Format rules:
- If the path points to a file with an extension, the format is inferred unless `file_format`
  is set in dataset params.
- If the path is a directory or has no extension, `file_format` must be set in dataset params.

All other listing connector params (`hive_partitioning_enabled`, `schema_source_path`,
`file_compression_type`, etc.) are taken from the dataset params.

## Examples

Spicepod:

```yaml
datasets:
  - from: s3://my-bucket/some-prefix/
    name: my_table
    params:
      dynamic: "true"
      file_format: parquet
```

Queries:

```sql
SELECT * FROM my_table('some_file.parquet');
```

```sql
SELECT * FROM my_table('s3://my-bucket/some-prefix/some_file.parquet');
```

```sql
SELECT * FROM my_table('my-hive-table/');
```

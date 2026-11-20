# qdb-cache-buildkite-plugin

Caches files and directories in AWS S3 or Cloudflare R2. Caches are separated by pipeline, configured keys, and Git ref by default. Restore runs before the command. Successful commands publish changed snapshots afterwards.

## Requirements

The host agent must provide Bash and set `QDB_CICD_AGENT_PLUGIN_PYTHON` to the Python 3.9+ interpreter used to create the job's virtual environment and validate cleanup. Configure storage access as described in [Backend configuration](#backend-configuration).

## Usage

### Cache files and directories

```yaml
steps:
  - label: build
    command: ./build.sh
    plugins:
      - bureau14/qdb-cache#master:
          path:
            - build/Testing/Temporary/CTestCostData.txt
            - .cache
            - ~/.m2/repository
          keys:
            - key: os
              value: linux
            - key: compiler
              value: gcc15
```

### Restore without updating the stored cache

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: .cache
      save: false
```

Set `restore: false` for a step that should only update the stored cache.

### Disable branch fallback

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: .cache
      fallback: disabled
```

## Configuration reference

| Option | Default | Meaning |
| --- | --- | --- |
| `path` | Required | File or directory, or a nonempty list of disjoint paths. A relative path starts at the job checkout, `~/` starts at the agent user's home, and an absolute path selects that local location. Use forward slashes for portable configurations. Duplicate or overlapping roots, drive-relative paths, and parent traversal are rejected. |
| `keys` | Empty list | Key/value pairs selecting an independent cache. Keys must be nonempty and unique; values are strings and may be empty. |
| `git-ref` | `true` | Include the Git ref in cache identity. Disable to share a cache across branches and tags. |
| `restore` | `true` | Restore before the command. |
| `clear` | `true` | Remove eligible local files absent from a verified snapshot. If all refs miss, clear eligible contents of the configured roots. Set to `false` to preserve extra local files on a hit or miss. Applies when restore is enabled. |
| `save` | `true` | Create or update the stored cache after a successful command. |
| `fallback` | `default` | Try master then main after the current ref, or use `disabled` for only the current ref. |
| `exclude` | Empty list | Case-sensitive glob pattern or list of patterns relative to each cached directory; a file path uses its basename. |
| `force` | `false` | Republish an unchanged snapshot or replace a malformed manifest. A forced save reuses file references from a valid, compatible manifest. |
| `strict` | `false` | Fail the hook on operational errors. Configuration and setup errors always fail. |
| `retries` | `3` | Total SDK request attempts, including the first. |
| `connect-timeout` | `10` | Positive finite connection timeout in seconds for storage and SSM requests. |
| `read-timeout` | `60` | Positive finite read timeout in seconds for storage and SSM requests. |
| `debug` | `false` | Enable Bash tracing. |

Timeouts and retries apply per SDK request. If a payload download is interrupted after the response starts, the plugin makes one more GET for the same object key and restarts the download. Each of the two GET requests can use the configured SDK attempts.

For example, exclude lock files:

```yaml
path: .cache
exclude:
  - '*.lock'
```

## How it works

| Hook | Action |
| --- | --- |
| `post-checkout` | Prepare the job's Python environment. |
| `pre-command` | Restore the cache. |
| `post-command` | Update the stored cache after command success. |
| `pre-exit` | Remove the job's Python environment and temporary state, including after failures or when saving is disabled. |

A missing cache is normal. Operational errors are logged and annotated; the command continues unless `strict` is enabled.

### Cache identity and fallback

With destination `s3://ci-cache/buildkite`, pipeline `example`, branch `feature`, and the keys from the build example, the manifest lives at

```text
s3://ci-cache/buildkite/example/refs/heads/feature/cache/files-v3/compiler=gcc15/os=linux/manifest.json
```

Use `keys` to separate caches for different steps, operating systems, or toolchains. The plugin sorts pairs by key and percent-encodes each name and value. An empty list puts the manifest directly under `files-v3/`. Tags take precedence over branches.

By default, restore tries the current ref, then master and main. Set `fallback: disabled` to use only the current ref. Saving updates the current ref's cache.

With `git-ref: false`, branches and tags share one cache. For example, with no keys the manifest is `buildkite/example/cache/files-v3/manifest.json`, and restore tries only that identity.

### Snapshots

A snapshot stores files individually and records their paths, permissions, and checksums in a manifest. It includes empty directories. Saving skips symlinks and uploads only file contents missing from the previous compatible snapshot. Once all uploads succeed, the plugin publishes the manifest if another writer hasn’t changed it. A competing save that loses this race skips publication.

Each configured path is optional at save time. If one is missing, the plugin saves the remaining eligible files and directories and drops the missing path's entries from the new snapshot. If every path is missing or excluded, it skips the save and preserves the previous snapshot. An error reading an input aborts publication.

Restore reuses matching local files and verifies each download’s size and SHA-256 checksum before changing the cache. By default, it removes stale files and preserves exclusions. If all refs miss, it clears eligible local contents. Set `clear: false` to keep extra local files on both hits and misses.

Cache files must have a single hard link. Restore rejects symlinked destinations and ambiguous filenames. A failed download or corrupt snapshot leaves local files intact while restore tries the next ref. With `strict: true`, the first error stops restore. A filesystem error during installation can leave a partial restore.

### Rollout and retention

Existing checkout-only snapshots in `files-v3/` remain readable. Before upgrading, set `clear: false` in steps that depend on local files surviving a cache miss or a restore from a smaller snapshot. When jobs share an identity, upgrade all writers so that each uses conditional publication. Check bucket policies allow conditional manifest writes.

Keep completed payloads outside age-based expiration rules. A newly published manifest may reference an old generation because reusing a payload leaves its age unchanged. For the example identity without keys, check whether your lifecycle rules match `buildkite/example/refs/heads/feature/cache/files-v3/objects/<generation>/<sha256>`. A bucket-wide rule or a rule on the shared `buildkite/` prefix also applies. Incomplete multipart uploads can expire independently. [S3 lifecycle rules](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro-lifecycle-rules.html) · [R2 object lifecycles](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)

A failed or competing save can leave uploaded objects outside the current manifest. Older manifests held by active readers and in-flight saves can also reference older generations. If an object expires or is removed, a restore that needs it reports an error. Even a forced save trusts a valid manifest's references. To reseed from a complete local cache, use a fresh cache key or remove the affected manifest before saving again.

## Backend configuration

Storage uses the configuration from `qdb-artifacts-buildkite-plugin`. Environment overrides take precedence over SSM parameters. SSM uses `AWS_DEFAULT_REGION`, defaulting to `eu-west-1`.

An S3 agent using its IAM role can supply:

```yaml
env:
  ARTIFACTS_BACKEND: s3
  ARTIFACTS_DESTINATION: s3://ci-cache/buildkite
  AWS_DEFAULT_REGION: eu-west-1
```

| Environment variable | SSM parameter | Meaning |
| --- | --- | --- |
| `ARTIFACTS_BACKEND` | `/services/buildkite/config/artifacts/object-store/backend` | `s3` or `r2`; defaults to `s3`. |
| `ARTIFACTS_DESTINATION` | `/services/buildkite/config/artifacts/object-store/destination` | Required `s3://bucket/prefix` or plain bucket name. |
| `ARTIFACTS_ENDPOINT_URL` | `/services/buildkite/config/artifacts/object-store/endpoint-url` | Optional endpoint override; SSM fallback applies to R2 only. |
| `ARTIFACTS_R2_ACCOUNT_ID` | `/services/buildkite/config/artifacts/object-store/r2/account-id` | R2 account ID, used to derive its default endpoint. |
| `ARTIFACTS_R2_ACCESS_KEY_ID` | `/services/buildkite/config/artifacts/object-store/r2/access-key-id` | R2 access key ID. |
| `ARTIFACTS_R2_SECRET_ACCESS_KEY` | `/services/buildkite/credentials/artifacts/r2/secret-access-key` | R2 secret access key, decrypted when read from SSM. |

S3 uses the agent's normal AWS credentials. R2 requires its account ID and access keys. SSM access requires `ssm:GetParameter` and applicable `kms:Decrypt` permissions.

S3 requires `s3:GetObject` and `s3:ListBucket` to read objects and distinguish missing keys from access denial. Saving also requires `s3:PutObject` and `s3:AbortMultipartUpload`. For buckets using SSE-KMS, allow `kms:Decrypt` for reads and multipart uploads, and `kms:GenerateDataKey` for writes. [S3 multipart permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html#mpuAndPermissions)

For R2, grant bucket-scoped Object Read access for restore and Object Read & Write for save. Bucket policies must allow conditional manifest writes, payload uploads, and multipart completion and abort.

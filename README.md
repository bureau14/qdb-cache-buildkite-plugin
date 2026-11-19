# qdb-cache-buildkite-plugin

Caches files and directories in AWS S3 or Cloudflare R2. Caches are separated by pipeline, configured keys, and Git ref by default. Restore runs before the command. Successful commands publish changed snapshots afterwards.

## Requirements

Bash and Python 3.9+ must be available on the host agent. Storage access must be configured as described in [Backend configuration](#backend-configuration).

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
| `path` | Required | Checkout-relative file or directory, or a nonempty list of disjoint paths. Use forward slashes for paths shared across platforms. Each agent resolves them under its own checkout. Duplicate roots, nested roots, absolute paths, home-relative paths, drive-qualified paths, and parent traversal are rejected. |
| `keys` | Empty list | Key/value pairs selecting an independent cache. Keys must be nonempty and unique; values are strings and may be empty. |
| `git-ref` | `true` | Include the Git ref in cache identity. Disable to share a cache across branches and tags. |
| `restore` | `true` | Restore before the command. |
| `save` | `true` | Create or update the stored cache after a successful command. |
| `fallback` | `default` | Try master then main after the current ref, or use `disabled` for only the current ref. |
| `exclude` | Empty list | Case-sensitive glob pattern or list of patterns relative to each cached directory; a file path uses its basename. |
| `force` | `false` | Republish an unchanged snapshot or replace a malformed manifest. A forced save reuses file references from a valid, compatible manifest. |
| `strict` | `false` | Fail the hook on operational errors. Configuration and setup errors always fail. |
| `retries` | `3` | Total SDK request attempts, including the first. |
| `connect-timeout` | `10` | Positive finite connection timeout in seconds for storage and SSM requests. |
| `read-timeout` | `60` | Positive finite read timeout in seconds for storage and SSM requests. |
| `debug` | `false` | Enable Bash tracing. |

Give each job exclusive use of its checkout and writable cache destinations through restore and build. Keep the plugin’s shared `.state` directory and the checkout in disjoint trees. The checkout itself may be a mount point. Provision it without an alias of private state at its root. A configured path cannot cross a mount below the checkout or contain a nested mount.

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

The manifest records the configured roots, exclusions, file and directory permissions, and a SHA-256 digest, byte length, and storage generation for each file. Each entry has one checkout-relative path. For example, `path: .cache` saves from the producing checkout and restores into the consuming checkout’s own `.cache` directory. Empty directories are included. Saving skips symlinks and inputs with symlinked ancestors. Missing required inputs or an entirely ineligible selection skip the save before backend configuration is needed.

On save, the plugin hashes each file. If its digest and size occur in the previous compatible manifest, the new snapshot reuses that file's object key without a storage request for the payload. For each new digest, the plugin verifies a private copy and uploads it under `objects/<generation>/<sha256>` beside the manifest. Each writer uses a fresh generation for its uploads. A permission change, rename within a cached directory, or file removal can update the manifest without uploading payloads.

Once all uploads succeed, the plugin publishes the manifest with one unconditional write. The last successful write wins, even if it comes from an older build. A reader finishes from the manifest it fetched. An upload failure leaves the previous manifest in place. If the store fails to confirm a manifest write, the hook reports an operational error.

Restore merges a compatible snapshot into the configured roots, preserving excluded and unrelated files. A different format or root/exclusion layout is a cache miss, including in strict mode. A miss leaves local files in place. Files with matching bytes avoid a download, including when only their permissions differ. Missing, different, or unreadable files are fetched and verified against the manifest's byte lengths and SHA-256 digests. Cache files must have a single hard link. A hard-linked input or matching destination path raises an operational error before publication or installation.

Save and restore reject sibling names that coincide under case folding or Unicode normalization, including implicit parent directories. Restore also checks the spelling of existing destination names, rejects traversal and symlinked destinations, and stages all required payloads before installing any of them. A corrupt snapshot, failed transfer, or denied request leaves local files intact while restore tries fallback refs. Strict mode stops on the first error. Once installation starts, a local filesystem error can leave a partial restore and ends fallback.

### Rollout and retention

The `files-v3/` namespace starts cold. The first successful save seeds it. Rolling back the plugin selects the earlier namespace. Before upgrading, check consuming pipelines for absolute, home-relative, overlapping, or mounted paths. Confirm that jobs sharing a cache key can accept publication in completion order and that bucket policies allow unconditional manifest writes.

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

For R2, grant bucket-scoped Object Read access for restore and Object Read & Write for save. Bucket policies must allow unconditional manifest writes, payload uploads, and multipart completion and abort.

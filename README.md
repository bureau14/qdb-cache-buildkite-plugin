# qdb-cache-buildkite-plugin

This Buildkite plugin restores files and directories from AWS S3 or Cloudflare R2 before a step runs. After the command succeeds, it saves the cache for subsequent builds.

## Requirements

The agent needs Bash and a Python 3.9+ interpreter. Set `QDB_CICD_AGENT_PLUGIN_PYTHON` to the interpreter’s path. Configure storage access as described in [Backend configuration](#backend-configuration).

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
| `git-ref` | `true` | Use the build’s tag or branch. Set a branch name or full ref to choose the cache explicitly, or use `false` to share across all refs. |
| `restore` | `true` | Restore before the command. |
| `clear` | `true` | Remove eligible local files absent from a verified snapshot. If all refs miss, clear eligible contents of the configured roots. Set to `false` to preserve extra local files on a hit or miss. Applies when restore is enabled. |
| `save` | `true` | Create or update the stored cache after a successful command. |
| `fallback` | `default` | Try `master` then `main` after the selected ref. Use `disabled` to try only the selected ref. |
| `exclude` | Empty list | Case-sensitive glob pattern or list of patterns relative to each cached directory; a file path uses its basename. |
| `force` | `false` | Publish the manifest even when the snapshot is unchanged. |
| `strict` | `false` | Fail the hook on operational errors. Configuration and setup errors always fail. |
| `parallelism` | Host CPU count (or `1` if unknown) | Maximum number of files uploaded or downloaded concurrently. Set a positive integer to limit transfer concurrency on shared agents. |
| `retries` | `3` | Maximum attempts per SDK request, including the first. |
| `connect-timeout` | `10` | Positive finite connection timeout in seconds for storage and SSM requests. |
| `read-timeout` | `60` | Positive finite read timeout in seconds for storage and SSM requests. |
| `debug` | `false` | Enable Bash tracing. |

To exclude lock files, use a quoted glob pattern.

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

A cache miss allows the build to continue. The plugin logs operational errors and annotates the Buildkite job. Set `strict: true` to fail the hook on an operational error.

### Cache identity and fallback

Within a pipeline, the configured path set, Git ref, and optional `keys` identify a cache. Path order and key order do not affect its identity. Use `keys` when steps cache the same paths but need separate contents, for example for different compilers.

With destination `s3://ci-cache/buildkite`, the build example on branch `feature` of pipeline `example` uses the following manifest. `<path-hash>` is the SHA-256 digest of the configured path set.

```text
s3://ci-cache/buildkite/example/refs/heads/feature/cache/v1/paths-<path-hash>/compiler=gcc15/os=linux/manifest.json
```

By default, a tag takes precedence over the build branch. Restore tries the selected ref first, then `master` and `main`. Save publishes to the selected ref. To have several branches read and update the same named cache, select it explicitly, for example with a variable supplied at pipeline upload time.

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: node_modules
      git-ref: "${CACHE_BRANCH}"
      fallback: disabled
```

With `CACHE_BRANCH=main`, those jobs read and write the `refs/heads/main` cache. Set `git-ref: false` to omit the ref from the cache key entirely.

### What gets saved

The plugin stores regular files individually and reuses identical contents from the last compatible snapshot. The snapshot includes empty directories, file and directory permissions, and symbolic links, including dangling links. Hard-linked names within the snapshot restore as links to the same file. A selected file whose other names are outside the snapshot restores as an independent file.

Missing paths are omitted. If nothing is eligible to save, the stored snapshot stays in place. The plugin publishes the manifest after all uploads succeed. If another job updates the manifest first, its snapshot wins.

### What restore changes

Restore reuses local files with matching contents and verifies each download’s size and SHA-256 checksum before installing it. If a manifest or download fails verification, restore tries the next configured ref unless `strict: true`.

By default, restore removes local entries absent from the snapshot and preserves excluded paths. If every ref misses, it clears eligible entries under the configured paths. Set `clear: false` to keep extra local files in either case. The agent needs write access to the affected directories. A filesystem error during installation can leave a partially restored tree.

## Backend configuration

Set the storage backend and bucket through agent environment variables or AWS Systems Manager Parameter Store (SSM). Environment variables take precedence. SSM uses `AWS_DEFAULT_REGION`, defaulting to `eu-west-1`.

For an S3 agent that uses its IAM role, the following environment selects the bucket and prefix.

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

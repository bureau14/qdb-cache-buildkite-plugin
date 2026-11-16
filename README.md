# qdb-cache-buildkite-plugin

Caches files and directories from the working directory in AWS S3 or Cloudflare R2. Caches are separated by pipeline, configured keys, and Git ref by default. Restore runs before the command; successful commands update the stored cache afterwards.

## Requirements

Bash and Python 3 must be available on the host agent. Storage access must be configured as described in [Backend configuration](#backend-configuration).

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
| `path` | Required | File or directory path, or a nonempty list of paths, relative to the working directory. |
| `keys` | Empty list | Key/value pairs selecting an independent cache. Keys must be nonempty and unique; values are strings and may be empty. |
| `git-ref` | `true` | Include the Git ref in cache identity. Disable to share a cache across branches and tags. |
| `restore` | `true` | Restore before the command. |
| `save` | `true` | Create or update the stored cache after a successful command. |
| `fallback` | `default` | Try master then main after the current ref, or use `disabled` for only the current ref. |
| `exclude` | Empty list | Case-sensitive glob pattern or list of patterns relative to each cached directory; a file path uses its basename. |
| `force` | `false` | Update the stored cache even when its checksum matches. |
| `strict` | `false` | Fail the hook on operational errors. Configuration and setup errors always fail. |
| `retries` | `3` | Total SDK request attempts, including the first. |
| `connect-timeout` | `10` | Positive finite connection timeout in seconds for storage and SSM requests. |
| `read-timeout` | `60` | Positive finite read timeout in seconds for storage and SSM requests. |
| `debug` | `false` | Enable Bash tracing. |

Cache destinations must be clean and writable before restore. Paths stay within the working directory. Timeouts and retries apply per SDK request; there is no overall operation deadline.

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

With destination `s3://ci-cache/buildkite`, pipeline `example`, branch `feature`, and no additional keys, the object is:

```text
s3://ci-cache/buildkite/example/refs/heads/feature/cache/v1/snapshot.tar.zst
```

Use `keys` to separate caches for different steps, operating systems, or toolchains. Tags take precedence over branches.

By default, restore tries the current ref, then master and main. Set `fallback: disabled` to use only the current ref. Saving updates the current ref's cache.

With `git-ref: false`, branches and tags restore and update the same cache for the pipeline and configured keys. Ref fallback is ignored.

### Snapshots

Snapshots preserve working-directory-relative paths, file contents, and permissions. Modification times are excluded from the checksum; unchanged snapshots skip uploading. Regular files and directories are supported. Saving skips symbolic links and input paths with symlinked ancestors. Restore rejects symlinked destination components.

Missing inputs or snapshots with no files skip saving. Restore preserves unrelated files. An interrupted restore can leave partially updated files, and concurrent saves can overwrite each other.

Older snapshots with a different path layout are skipped and refreshed on the next save.

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

S3 requires `s3:GetObject`, `s3:ListBucket` to distinguish missing objects from access denial, and object-write/multipart permissions for saving. The plugin creates neither buckets nor credentials.

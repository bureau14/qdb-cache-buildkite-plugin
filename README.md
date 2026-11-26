# qdb-cache-buildkite-plugin

Cache files and directories between Buildkite jobs using AWS S3 or Cloudflare R2. The plugin restores the cache before the step's command runs and saves changes after the command succeeds.

## Usage

The agent needs Bash, Python 3.9 or later, and access to the storage bucket. Set `QDB_CICD_AGENT_PLUGIN_PYTHON` to the Python executable. See [Backend configuration](#backend-configuration) for storage settings.

To cache Maven dependencies and a build output file, list their paths in the step:

```yaml
steps:
  - label: build
    command: ./build.sh
    plugins:
      - bureau14/qdb-cache#master:
          path:
            - ~/.m2/repository
            - build/Testing/Temporary/CTestCostData.txt
```

Use Unix-style paths with forward slashes (`/`), including on Windows. Relative paths start at the checkout. For example, `.cache` is inside the checkout, `../relative` is a sibling directory, and `~/.m2` is in the agent user's home. Absolute paths also work. On Windows, include the slash after the drive letter, as in `C:/cache`.

List each path once. If you list a directory, its contents are already included. To cache a single path, write `path: .cache`. This example restores that directory, skips lock files, and keeps the stored cache unchanged:

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: .cache
      exclude: '*.lock'
      save: false
```

## Configuration reference

| Option | Default | Meaning |
| --- | --- | --- |
| `path` | Required | List of files or directories to cache. |
| `key` | Omitted | Cache key for restore and save. Omit it to share a cache by pipeline and paths alone. |
| `restore-key` | Omitted | Optional fallback key to try during restore. |
| `restore` | `true` | Restore the cache before the command runs. |
| `clear` | `true` | During restore, delete local files missing from the cache. If no cache exists, clear the listed paths. Excluded files are kept. |
| `save` | `true` | Update the cache after the command succeeds. |
| `exclude` | `[]` | Skip paths matching a glob pattern or list of patterns. Patterns are case-sensitive and relative to each cached directory. For a single file, they match its name. |
| `force` | `false` | Write the manifest even when the cache contents are unchanged. |
| `strict` | `false` | Fail the hook on save or restore errors. Setup and configuration errors always fail the hook. |
| `parallelism` | Agent CPU count | Maximum number of files to transfer at once. The default is 1 if the CPU count is unknown. |
| `retries` | `3` | Maximum attempts per storage or SSM request, including the first attempt. |
| `connect-timeout` | `10` | Seconds to wait for a storage or SSM connection. |
| `read-timeout` | `60` | Seconds to wait for a read from storage or SSM. |
| `debug` | `false` | Enable Bash tracing for the hooks. |

## Sharing a cache

Within a pipeline, steps share a cache when their configured paths and key match. Path order doesn't matter. To scope a cache to a branch, include `${BUILDKITE_BRANCH}` in `key`. The example uses the master branch’s key as a restore fallback.

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: ~/.m2/repository
      key: "m2-jdk17-${BUILDKITE_BRANCH}"
      restore-key: "m2-jdk17-master"
```

Restore tries the branch's key first, then the fallback key. Save always writes to the branch's key.

The manifest lists the files in the cache. For pipeline `example` on branch `feature`, the example above stores it at the following location. The path hash is the SHA-256 of the configured paths.

```text
s3://ci-cache/buildkite/example/cache/v1/paths-<path-hash>/key=m2-jdk17-feature/manifest.json
```

## How restore and save work

**Restore.** The plugin reuses local files whose contents match the cache. For each download, it checks the size and SHA-256 checksum before installing the file. If the manifest or a download fails verification, it tries the fallback key if configured, unless `strict: true`.

With the default `clear: true`, restore removes extra files from the listed paths. If no cache matches, it clears those paths and the build continues. Excluded files stay in place. Set `clear: false` to keep extra local files even on a miss. The agent needs write access to the directories it restores. A filesystem error during installation can leave some files restored and others untouched.

**Save.** The plugin uploads files individually and reuses identical content from the previous compatible cache. All writers must finish before save starts. Files, links, and their parent directories must stay unchanged until uploads finish. If a file changes between hashing and upload, the stored bytes can differ from the manifest checksum and later restores can fail verification.

The cache preserves empty directories, file and directory permissions, and symbolic links, including links whose targets are missing. Hard links within the cache still point to the same file after restore. If only one name for a hard-linked file is cached, it restores as an independent file.

Save skips missing paths. If there is nothing to save, it keeps the previous cache. It writes the manifest only after all uploads succeed. If another job updates the manifest first, that job's cache wins.

**Hooks.** `post-checkout` prepares the job's Python environment, `pre-command` restores the cache, and `post-command` saves it after a successful command. The `pre-exit` hook removes the environment and temporary files, including after a failure or when saving is disabled. The plugin logs cache errors and adds an annotation to the Buildkite job.

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

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
| `keys` | `[]` | Give steps separate caches, for example by compiler or OS. |
| `git-ref` | `true` | Use the build's tag or branch. Set a branch name or full Git ref to choose one, or `false` to share across refs. |
| `restore` | `true` | Restore the cache before the command runs. |
| `clear` | `true` | During restore, delete local files missing from the cache. If no cache exists, clear the listed paths. Excluded files are kept. |
| `save` | `true` | Update the cache after the command succeeds. |
| `fallback` | `default` | Try `master`, then `main`, if the selected ref misses. Set to `disabled` to skip fallback. |
| `exclude` | `[]` | Skip paths matching a glob pattern or list of patterns. Patterns are case-sensitive and relative to each cached directory. For a single file, they match its name. |
| `force` | `false` | Write the manifest even when the cache contents are unchanged. |
| `strict` | `false` | Fail the hook on save or restore errors. Setup and configuration errors always fail the hook. |
| `parallelism` | Agent CPU count | Maximum number of files to transfer at once. The default is 1 if the CPU count is unknown. |
| `retries` | `3` | Maximum attempts per storage or SSM request, including the first attempt. |
| `connect-timeout` | `10` | Seconds to wait for a storage or SSM connection. |
| `read-timeout` | `60` | Seconds to wait for a read from storage or SSM. |
| `debug` | `false` | Enable Bash tracing for the hooks. |

## Sharing a cache

Within a pipeline, steps share a cache when their paths, Git ref, and `keys` match. The order of paths and keys doesn't matter. To keep separate caches for different compilers, add a key:

```yaml
path: .cache
keys:
  - key: compiler
    value: gcc15
```

Each key needs a unique, nonempty name and a string value. An empty string is a valid value.

By default, the plugin uses the build's tag if there is one, otherwise its branch. Restore can fall back to `master` and `main`. Save always writes to the selected ref. To have jobs on different branches read and update the same branch cache, set `git-ref` explicitly:

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: node_modules
      git-ref: "master"
      fallback: disabled
```

To share one cache across all branches and tags, use `git-ref: false`.

The manifest lists the files in the cache. For pipeline `example` on branch `feature`, the compiler example above stores it at the following location. The path hash is the SHA-256 of the configured paths.

```text
s3://ci-cache/buildkite/example/refs/heads/feature/cache/v1/paths-<path-hash>/compiler=gcc15/manifest.json
```

## How restore and save work

**Restore.** The plugin reuses local files whose contents match the cache. For each download, it checks the size and SHA-256 checksum before installing the file. If the manifest or a download fails verification, it tries the next fallback unless `strict: true`.

With the default `clear: true`, restore removes extra files from the listed paths. If every ref misses, it clears those paths and the build continues. Excluded files stay in place. Set `clear: false` to keep extra local files even on a miss. The agent needs write access to the directories it restores. A filesystem error during installation can leave some files restored and others untouched.

**Save.** The plugin uploads files individually and reuses identical content from the previous compatible cache. Keep files, links, and their parent directories stable while saving. If a file changes after the plugin hashes it, the uploaded file can fail verification on restore.

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

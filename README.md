# qdb-cache-buildkite-plugin

Caches literal files and directories in AWS S3 or Cloudflare R2. Each pipeline, Git ref, and optional set of key/value dimensions owns one replaceable `snapshot.tar.zst`. Restore runs before the command; save runs after an explicitly successful command. Storage uses the QDB artifacts plugin's existing `ARTIFACTS_*` configuration and SSM parameters with a separate `cache/v1` subtree.

## What it does

Restores a file/directory snapshot before the command and refreshes it after success. Each pipeline, Git ref, and optional set of dimensions owns one object; restored files merge into existing destinations.

## Requirements

Bash and Python 3 must be available on the host agent. Setup installs `boto3` and `zstandard`; runtime support depends on their availability for the agent's Python and platform. The agent needs object-store permissions and the shared artifacts backend configuration described below.

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
            - .cache/ccache
          keys:
            - key: os
              value: linux
            - key: compiler
              value: gcc-14
```

### Restore without saving

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: .cache/ccache
      save: false
```

Set `restore: false` for a step that should only save. Both operations disabled still prepare the Python environment and parse configuration.

### Disable branch fallback

```yaml
plugins:
  - bureau14/qdb-cache#master:
      path: .cache/ccache
      fallback: disabled
```

## Configuration reference

Plugin options are declared in `plugin.yml` and parsed before storage access, including when both operations are disabled. The post-command hook skips saving before invoking Python when the command failed or its status is absent. Buildkite supplies `BUILDKITE_PLUGIN_QDB_CACHE_*` variables. Hyphenated names use underscores; string lists use contiguous zero-based indexes. Dimensions use records such as `BUILDKITE_PLUGIN_QDB_CACHE_KEYS_0_KEY` and `BUILDKITE_PLUGIN_QDB_CACHE_KEYS_0_VALUE`, retaining original dimension key spelling in their values. Boolean and scalar/indexed-list parsing follow the artifacts plugin's `_get_env_bool` and `_get_env_array` helpers; dimension-pair parsing is cache-specific. For booleans, whitespace is stripped, `true`/`1`/`on`/`yes` enable an option, and other supplied values disable it.

| Option | Default | Meaning |
| --- | --- | --- |
| `path` | Required | Nonempty string or list of literal file/directory paths combined into one snapshot. No globs or shell evaluation. |
| `keys` | Empty list | Objects with a nonempty string `key` and string `value`. Empty values are allowed; duplicate keys are rejected. |
| `restore` | `true` | Restore before the command. |
| `save` | `true` | Save after an explicitly successful command. |
| `fallback` | `default` | Try master then main after the current ref, or use `disabled` for only the current ref. |
| `exclude` | Empty list | String or list of case-sensitive `fnmatchcase` patterns relative to each directory root; a file root uses its basename. |
| `force` | `false` | Upload despite a matching checksum. Does not bypass command success, validation, or missing inputs. |
| `strict` | `false` | Fail the hook on operational errors. Configuration/setup failures always fail. |
| `retries` | `3` | Positive integer total SDK request attempts including the first; standard retry policy without an outer retry loop. |
| `connect-timeout` | `10` | Positive connection timeout in seconds for storage and SSM clients. |
| `read-timeout` | `60` | Positive response read timeout in seconds for storage and SSM clients. |
| `debug` | `false` | Inherited Bash tracing around hook execution. |

Connection/read timeouts and retries apply to individual SDK requests. Restore and save run directly in the hook process without an overall operation deadline.

Relative paths resolve against the hook working directory. Explicit `~` paths expand locally while keeping the logical home specification independent of agent usernames. Absolute paths are supported. Configure distinct, nonoverlapping destinations containing regular files and directories. Paths and job state are trusted; there are no setup checks for filesystem roots, overlapping destinations, symlinks or Windows junctions in the state tree. Do not cache a checkout containing the plugin's own `.state` directory.

Exclusions use POSIX `/` separators. `fnmatchcase` gives `**` no special recursive meaning, and `*` can match `/`. A matched directory is pruned with all descendants. Exclusions affect layout compatibility:

```yaml
path: .cache
exclude:
  - '*.lock'
  - 'compiler/tmp'
  - 'downloads/*.partial'
```

## How it works

| Hook | Trigger | Action |
| --- | --- | --- |
| `post-checkout` | After checkout | Creates the job-specific Python environment and installs dependencies. |
| `pre-command` | Before the command | Restores the current ref or its master/main fallback. |
| `post-command` | After an explicitly successful command | Saves a changed snapshot for the current ref. |

`post-checkout` discovers Python through `QDB_CICD_AGENT_PYTHON3`, then `python3`/`python` on PATH, then inherited Windows install-directory fallbacks. It recreates `.state/<BUILDKITE_JOB_ID>/.venv` using `python -m venv --upgrade-deps` and installs unpinned `boto3` and `zstandard` using the inherited pip retry flags. Linux/macOS/FreeBSD use `bin`; Windows/Git Bash uses `Scripts`. Setup failures are fatal. Runtime support follows dependency availability for the agent's Python and platform.

`pre-command` restores; `post-command` parses configuration and saves only after command success. Both operations run directly in the hook process and remove temporary directories on completion or handled errors. Different jobs use separate environments under the plugin checkout's `.state` directory. There is no `pre-exit` hook; environments remain until workspace cleanup removes them. Abrupt process/machine termination can leave temporary directories or siblings; broad pruning is not performed.

| Event | Result |
| --- | --- |
| All candidates missing or incompatible | Successful miss; destinations untouched; no annotation. |
| Disabled operation, failed command, missing inputs, unchanged save | Successful skip; no cache-error annotation. |
| Configuration parsing errors or storage configuration rejected with `ValueError` | Fatal hook failure and best-effort annotation. |
| Other SDK errors, authorization, network, corruption, archive/merge I/O | Log and annotate; continue by default, fail under strict mode. |
| Candidate error before merge | Normal mode may try another candidate; strict stops immediately. |
| Error after merge starts | Stop restore; do not merge another candidate. |
| Annotation failure | Log reporting failure and retain the original result. |

Diagnostics include identity, source/target refs, outcome, transferred bytes, and duration. Error annotations use job-scoped `qdb-cache-restore` and `qdb-cache-save` contexts and stdin bodies. Restore and save errors do not overwrite each other. Command failures themselves are not annotated by the cache plugin.

### Identity and fallback

With destination `s3://ci-cache/buildkite`, pipeline `example`, branch `feature`, and the dimensions above, the object is `s3://ci-cache/buildkite/example/refs/heads/feature/cache/v1/compiler=gcc-14/os=linux/snapshot.tar.zst`.

Pairs are sorted case-sensitively by original key. Keys and values are separately URL-encoded so `/`, `=`, and `%` cannot change path structure. Reordering pairs selects the same object; changing a value selects a different namespace. Without pairs, `snapshot.tar.zst` is directly under `cache/v1/`.

Identity includes no automatic step, OS, architecture, compiler, dependency digest, or cache name. Pipelines supply dimensions for workload compatibility and independent snapshots. Steps caching different layouts should supply separate `name` values; otherwise they overwrite the same object and fail each other's layout check.

`BUILDKITE_TAG` takes priority over `BUILDKITE_BRANCH`; tags use `refs/tags/...`, branches use `refs/heads/...`. Pipeline, Git ref, and `BUILDKITE_JOB_ID` are required. Buildkite-provided identifiers are used without additional character or path validation.

Restore always tries the current ref first. Default fallback then tries `refs/heads/master` and `refs/heads/main`, omitting the current ref if it is already one of them. Use `fallback: disabled` for only the current ref. Custom fallback branches are unsupported.

Fallback retains pipeline, dimensions, and path/exclusion layout. A fallback hit can seed the current branch; save always targets the current ref. No bucket listing or build-history lookup occurs.

### Snapshots and merge

Normalized logical roots are sorted and stored as `roots/0000`, `roots/0001`, and so on. Identical basenames in different directories remain distinct. The current configuration maps archive roots to local destinations. A layout SHA-256 covers logical path specifications and exclusions; a changed layout makes an old snapshot incompatible instead of redirecting it.

The tar uses standard `tarfile` traversal and includes file bytes, names/types, permission bits, and modification times. Ownership/user/group names are normalized for stable checksums. Access/creation times, special permission bits, ACLs, and extended attributes are omitted. Hard-linked files become independent regular entries. Included symbolic links are rejected; exclusions can omit them. Restore accepts regular files and directories only.

Extraction checks member paths stay within staging and map to configured roots. Inputs are assumed to be ordinary, stable files; the plugin does not impose additional portable-filename or archive-metadata policies. Dimensions own cross-platform compatibility.

The uncompressed tar's SHA-256 includes restored metadata, so mtime changes can change the checksum. `qdb-cache-format`, `qdb-cache-layout-sha256`, and `qdb-cache-sha256` are stored as user metadata on the same S3/R2 object as `snapshot.tar.zst`, through the upload's `Metadata` argument. There is no separate metadata object. Restore checks format and layout from the GET response; save compares all three fields through HEAD. The checksum identifies unchanged snapshots during save; restore does not verify it. The layout digest prevents positional archive roots from mapping old content into a changed destination layout. ETags are not used as content checksums. Unchanged checks still read local inputs; they save compression/upload traffic.

Restore binds bytes and metadata to one streamed GET response, decompresses with `zstandard`, and validates member paths/types before extracting into staging. Only then does live merge start. Matched files use temporary siblings and `os.replace`; unmatched existing files remain. Destination symlinks are refused; file/directory conflicts surface as filesystem errors during merge. Directory metadata is applied after children. A miss creates no destinations. There is no clean/replacement mode.

Merge is not transactional across files or roots. A file/directory conflict, permission error, disk failure, or interruption after installation starts can leave earlier files updated. Per-file replacement does not provide rollback. Merge can retain stale files absent from the snapshot.

Save requires `BUILDKITE_COMMAND_EXIT_STATUS` to equal `0`. Failed or missing status skips publication. Any missing root skips the whole snapshot before resolving credentials. Fully excluded payloads also skip; a naturally empty configured directory is valid. Keep inputs stable during capture; it is not a filesystem-wide atomic snapshot.

Save compares only the current ref's metadata. Matching format, layout and content digest skip compression/upload unless forced; missing/mismatched metadata causes upload. Force does not override failed commands, missing inputs, exclusions, or validation errors.

Each identity has one current object. Concurrent jobs use ordinary backend overwrite behavior without locks or build-number ordering. An older build can overwrite a newer build; changes can be lost. A lost upload response can mean a complete replacement committed despite the reported client error. Bucket versioning and retention are external.

### Capacity and reset

Snapshots read all included inputs. Provision scratch for the uncompressed tar, compressed object, and staged content in addition to live data. There is no arbitrary cache-size limit or measured throughput/capacity claim.

Externally deleting the selected object gives a cold cache. Changing dimensions selects another namespace; force refreshes the existing identity. `cache/v1` starts cold, with no migration from artifact/test-report objects or upstream cache keys. The plugin manages neither history nor pruning.

## Backend configuration

Storage must already exist and be configured through SSM or environment overrides. An S3 agent using its IAM role can supply:

```yaml
env:
  ARTIFACTS_BACKEND: s3
  ARTIFACTS_DESTINATION: s3://ci-cache/buildkite
  AWS_DEFAULT_REGION: eu-west-1
```

Example bucket names describe configuration, not provisioned infrastructure. The plugin creates neither buckets nor credentials. An omitted optional `ARTIFACTS_DOMAIN` still triggers an inherited SSM lookup; environment-configured storage does not inherently remove the AWS/SSM dependency.

Precedence is a nonempty environment override, then the matching SSM parameter, then an inherited default where one exists. SSM uses `AWS_DEFAULT_REGION`, defaulting to `eu-west-1`.

| Environment variable | SSM parameter | Behavior |
| --- | --- | --- |
| `ARTIFACTS_BACKEND` | `/services/buildkite/config/artifacts/object-store/backend` | `s3` or `r2`; defaults to `s3`. |
| `ARTIFACTS_DESTINATION` | `/services/buildkite/config/artifacts/object-store/destination` | Required `s3://bucket/prefix` or legacy plain bucket. R2 also uses the S3 URI form. |
| `ARTIFACTS_ENDPOINT_URL` | `/services/buildkite/config/artifacts/object-store/endpoint-url` | Explicit endpoint override. SSM endpoint fallback is consulted for R2; S3 uses only the environment override. |
| `ARTIFACTS_R2_ACCOUNT_ID` | `/services/buildkite/config/artifacts/object-store/r2/account-id` | Required for R2; derives the endpoint if no override is supplied. |
| `ARTIFACTS_R2_ACCESS_KEY_ID` | `/services/buildkite/config/artifacts/object-store/r2/access-key-id` | Required R2 access key ID. |
| `ARTIFACTS_R2_SECRET_ACCESS_KEY` | `/services/buildkite/credentials/artifacts/r2/secret-access-key` | Required R2 secret; SSM is read with decryption. |
| `ARTIFACTS_DOMAIN` | `/services/buildkite/config/artifacts/object-store/r2/artifacts-domain` | Inherited optional lookup; caches do not generate public URLs. |

S3 uses boto3's normal AWS credential chain, typically the agent IAM role. R2 uses stored S3-compatible credentials, `s3v4` signatures, path-style addressing, region `auto`, and a default endpoint of `https://<account-id>.r2.cloudflarestorage.com`. Placeholder R2 settings are rejected. There is no standalone-R2 mode that bypasses inherited AWS/SSM resolution.

SSM reads require `ssm:GetParameter` for consulted parameters and `kms:Decrypt` where applicable. An environment override skips its own lookup; omitted optional settings can still require SSM. Missing parameters and failed authorization/network requests are distinguished.

Restore requires object-read access (`s3:GetObject` on S3). Save reads metadata with HEAD and uses boto3's multipart-capable upload manager, requiring object-write and applicable multipart permissions. The plugin does not list buckets or delete objects. Scope credentials to the intended destination; configure lifecycle expiration for unfinished multipart uploads and inactive snapshots externally.

## Debug mode

Set `debug: true` in the plugin configuration to enable inherited Bash tracing around hook execution.

```yaml
debug: true
```

# SaladCloud API Knowledge Setup for Cline

This setup gives **Cline** a local, machine-readable copy of SaladCloud's official documentation and OpenAPI specifications so it can build code that calls the Salad API correctly.

The approach is intentionally **not a web scrape**.

Instead, it pulls the official SaladCloud documentation repository:

`https://github.com/SaladTechnologies/salad-cloud-docs`

This gives Cline three useful layers of information:

1. **OpenAPI specifications** — exact endpoints, methods, parameters, schemas, and enums.
2. **API reference documentation** — explanations and examples.
3. **Product/workflow documentation** — how Salad APIs are intended to be used.

---

## 1. Target project structure

For an Ubuntu project located at:

```text
/home/you/my-project/
```

the final structure should look approximately like:

```text
/home/you/my-project/
├── .clinerules/
│   └── salad-api.md
│
├── docs/
│   └── salad/
│       ├── README.md
│       ├── SOURCE_COMMIT
│       ├── SALAD_DOCS_CLAUDE.md
│       ├── docs.json
│       ├── index.mdx
│       ├── api-specs/
│       ├── reference/
│       ├── general/
│       ├── container-engine/
│       ├── transcription/
│       ├── storage/
│       ├── gateway-service/
│       └── ai-gateway/
│
└── scripts/
    └── sync-salad-docs.sh
```

The important distinction is:

```text
.clinerules/salad-api.md
        ↓
  Instructions for Cline

docs/salad/
        ↓
 Actual Salad documentation
```

**Do not put the entire Salad documentation inside `.clinerules/`.**

The `.clinerules` file should contain concise instructions telling Cline how to use the documentation.

---

# 2. Create the directories

From your project root:

```bash
cd /home/you/my-project

mkdir -p .clinerules
mkdir -p docs/salad
mkdir -p scripts
```

If your project is somewhere else, substitute the appropriate path.

---

# 3. Create the Salad documentation sync script

Create:

```text
scripts/sync-salad-docs.sh
```

For example:

```bash
nano scripts/sync-salad-docs.sh
```

Paste the following:

```bash
#!/usr/bin/env bash

set -euo pipefail

REPO_URL="https://github.com/SaladTechnologies/salad-cloud-docs.git"
DEST_DIR="${DEST_DIR:-docs/salad}"
TMP_DIR="$(mktemp -d)"

cleanup() {
    rm -rf "$TMP_DIR"
}

trap cleanup EXIT

echo "==> Fetching SaladCloud documentation..."

git clone \
    --depth 1 \
    "$REPO_URL" \
    "$TMP_DIR/salad-cloud-docs"

REPO_DIR="$TMP_DIR/salad-cloud-docs"
COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"

echo "==> Salad documentation commit: $COMMIT"

echo "==> Installing API specs and relevant documentation..."

rm -rf "$DEST_DIR"

mkdir -p "$DEST_DIR"

# ------------------------------------------------------------
# API specifications
# ------------------------------------------------------------

cp -R \
    "$REPO_DIR/api-specs" \
    "$DEST_DIR/api-specs"

# ------------------------------------------------------------
# Main API reference
# ------------------------------------------------------------

cp -R \
    "$REPO_DIR/reference" \
    "$DEST_DIR/reference"

# ------------------------------------------------------------
# Product documentation
# ------------------------------------------------------------

for dir in \
    general \
    container-engine \
    transcription \
    storage \
    gateway-service \
    ai-gateway
do
    if [[ -d "$REPO_DIR/$dir" ]]; then
        cp -R \
            "$REPO_DIR/$dir" \
            "$DEST_DIR/$dir"
    fi
done

# ------------------------------------------------------------
# Agent-oriented / repository documentation
# ------------------------------------------------------------

if [[ -f "$REPO_DIR/CLAUDE.md" ]]; then
    cp "$REPO_DIR/CLAUDE.md" \
       "$DEST_DIR/SALAD_DOCS_CLAUDE.md"
fi

if [[ -f "$REPO_DIR/docs.json" ]]; then
    cp "$REPO_DIR/docs.json" \
       "$DEST_DIR/docs.json"
fi

if [[ -f "$REPO_DIR/index.mdx" ]]; then
    cp "$REPO_DIR/index.mdx" \
       "$DEST_DIR/index.mdx"
fi

# ------------------------------------------------------------
# Record exact source revision
# ------------------------------------------------------------

cat > "$DEST_DIR/SOURCE_COMMIT" <<EOF
Repository: $REPO_URL
Commit: $COMMIT
Fetched: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

# ------------------------------------------------------------
# Create local README
# ------------------------------------------------------------

cat > "$DEST_DIR/README.md" <<EOF
# SaladCloud Documentation

Local copy of the official SaladCloud documentation repository.

Source:
$REPO_URL

Commit:
$COMMIT

Fetched:
$(date -u +"%Y-%m-%dT%H:%M:%SZ")

## API specifications

The files under \`api-specs/\` are the primary machine-readable API
contracts.

Use them as the source of truth for:

- HTTP methods
- endpoint paths
- parameters
- request bodies
- response schemas
- required fields
- enums
- data types

## Documentation

The following directories contain supporting documentation:

- reference/
- container-engine/
- transcription/
- storage/
- gateway-service/
- ai-gateway/
- general/

## Important

Do not invent Salad API endpoints, parameters, fields, or enum values.

When implementing API integrations:

1. Consult api-specs/.
2. Consult reference/.
3. Consult the relevant product documentation.
4. Verify current state using the live Salad API.
EOF

echo
echo "=========================================="
echo " SaladCloud documentation synchronized"
echo "=========================================="
echo
echo "Commit:"
echo "  $COMMIT"
echo
echo "Destination:"
echo "  $DEST_DIR"
echo
echo "OpenAPI specifications:"
find "$DEST_DIR/api-specs" \
    -maxdepth 1 \
    -type f \
    -print \
    | sort
echo
```

Save and exit.

Then:

```bash
chmod +x scripts/sync-salad-docs.sh
```

---

# 4. Download the Salad documentation

Run:

```bash
./scripts/sync-salad-docs.sh
```

You should see something similar to:

```text
==> Fetching SaladCloud documentation...
==> Salad documentation commit: 123456789...
==> Installing API specs and relevant documentation...

==========================================
 SaladCloud documentation synchronized
==========================================

Commit:
  123456789...

Destination:
  docs/salad

OpenAPI specifications:
docs/salad/api-specs/...
```

The earlier version of this script used Git sparse-checkout incorrectly with root-level files such as `index.mdx`. This version intentionally uses a shallow clone instead, which is simpler and avoids that problem.

---

# 5. Verify the API specifications

Run:

```bash
find docs/salad/api-specs -maxdepth 1 -type f -print | sort
```

You should see Salad's OpenAPI specifications, including the main SaladCloud API and the other public APIs.

These specifications are the most important files for Cline because they provide the machine-readable API contract.

Cline should use them to determine:

- HTTP methods
- URL paths
- parameters
- request bodies
- response schemas
- required fields
- enums
- data types

---

# 6. Create the Cline rules

Cline's project-specific rules should live under:

```text
.clinerules/
```

For this project, create:

```text
.clinerules/salad-api.md
```

On Ubuntu:

```bash
nano .clinerules/salad-api.md
```

Paste:

```md
# SaladCloud API Development Rules

These rules apply whenever working with the SaladCloud API.

## Primary objective

Build SaladCloud API integrations that are correct according to Salad's
current API contract and documentation.

Do not guess Salad API behavior.

---

## Local documentation

The official SaladCloud documentation is available locally at:

    docs/salad/

The most important resources are:

    docs/salad/api-specs/
    docs/salad/reference/
    docs/salad/general/
    docs/salad/container-engine/
    docs/salad/transcription/
    docs/salad/storage/
    docs/salad/gateway-service/
    docs/salad/ai-gateway/

---

## Source-of-truth hierarchy

Use sources in this order:

1. OpenAPI specifications in `docs/salad/api-specs/`
2. API reference in `docs/salad/reference/`
3. Relevant Salad product documentation
4. Other local Salad documentation

The OpenAPI specifications are authoritative for the API contract.

This includes:

- HTTP methods
- URL paths
- path parameters
- query parameters
- headers
- request bodies
- required fields
- response bodies
- enums
- data types

Never invent an endpoint, field, parameter, enum, or request schema.

---

## Before writing an API call

Before implementing a Salad API request:

1. Find the operation in the appropriate OpenAPI specification.
2. Verify the HTTP method.
3. Verify the complete path.
4. Verify required path parameters.
5. Verify query parameters.
6. Verify required headers.
7. Verify the request body schema.
8. Verify response schemas.
9. Check the relevant Salad documentation for workflow requirements.

If the requested operation cannot be found in the OpenAPI specification,
do not fabricate it.

Instead:

- search the local documentation;
- if still unavailable, tell the user that the operation could not be
  verified.

---

## SaladCloud API

The primary SaladCloud API base URL is:

    https://api.salad.com

Authentication uses:

    Salad-Api-Key: $SALAD_API_KEY

Never hard-code API keys.

Never commit API keys.

Never print API keys in logs.

Use environment variables or the project's existing secret-management
mechanism.

---

## Current state

Do not assume current SaladCloud state from documentation.

Documentation describes the API contract.

For current state, use the live API.

Examples of information that must not be guessed:

- organization IDs
- project names
- container group state
- job state
- endpoint state
- available resources
- quotas
- current configuration
- current URLs
- current model availability

When appropriate, GET/read the resource before modifying it.

---

## Mutating operations

For operations that modify Salad resources:

1. Identify the exact resource.
2. Read its current state when practical.
3. Verify the requested change against the OpenAPI schema.
4. Perform the mutation.
5. Read the resource again when practical.
6. Verify that the desired state was actually reached.

Do not blindly retry mutations.

Be particularly careful with:

- create operations
- delete operations
- job submission
- reallocation
- recreation
- restart operations

---

## Error handling

Use the documented HTTP status codes and response schemas.

Do not assume an error response has a particular shape unless the OpenAPI
specification defines it.

When implementing retries:

- prefer retries only for transient failures;
- do not blindly retry non-idempotent operations;
- honor `Retry-After` when supplied;
- use bounded retries.

---

## Code generation

When generating an API client:

- preserve Salad's exact field names;
- preserve enum values exactly;
- preserve nullable/optional distinctions;
- preserve required fields;
- preserve response types;
- don't simplify schemas in ways that change API behavior.

If an official Salad SDK exists for the language being used, consider using
it instead of manually constructing HTTP requests.

The OpenAPI specification remains the authoritative API contract.

---

## API vs Portal

Prefer API-based workflows when the user asks for automation or code.

Do not replace an API operation with manual Portal instructions when the API
supports the requested operation.

Portal-only information may be useful for discovering organization/project
context, but API calls should use the documented API.

---

## Credentials

Never:

- put `SALAD_API_KEY` in source code;
- commit `.env` files containing credentials;
- include API keys in examples;
- echo credentials to the terminal;
- include credentials in error reports.

Use:

    SALAD_API_KEY

from the environment or the project's secret manager.

---

## Documentation freshness

The local Salad documentation is synchronized from the official
SaladCloud documentation repository.

The synchronization metadata is stored in:

    docs/salad/SOURCE_COMMIT

If the API behavior appears inconsistent with the local documentation,
run:

    ./scripts/sync-salad-docs.sh

before assuming the API has changed.

---

## When uncertain

Do not hallucinate.

If an endpoint, field, enum, parameter, or workflow cannot be verified from
the local Salad documentation:

1. Search `docs/salad/api-specs/`.
2. Search `docs/salad/reference/`.
3. Search the relevant product documentation.
4. If still unresolved, explicitly state what could not be verified.

Correctness is more important than producing an immediate API call.
```

Save it.

---

# 7. Important: where Cline rules live on Ubuntu

The location is **relative to your project**, not a special Ubuntu-wide directory.

For example:

```text
/home/alice/projects/my-salad-app/
├── .clinerules/
│   └── salad-api.md
├── docs/
│   └── salad/
└── scripts/
```

If your project is:

```text
/home/alice/projects/my-salad-app
```

then the Cline rule is:

```text
/home/alice/projects/my-salad-app/.clinerules/salad-api.md
```

You do **not** need to put it in:

```text
/etc/
~/.config/
~/.cline/
```

or another Ubuntu system directory.

Keep the rules with the project.

This is also preferable for Git because the rules can be shared with other developers and Cline instances working on the same project.

---

# 8. Keep the documentation separate from the rules

Use this model:

```text
.clinerules/
└── salad-api.md
```

contains **instructions**.

While:

```text
docs/salad/
├── api-specs/
├── reference/
├── container-engine/
├── transcription/
└── ...
```

contains **knowledge**.

The Cline rule should tell Cline:

> "When you need to make a Salad API call, inspect the local OpenAPI specification first."

It should not contain the entire OpenAPI specification itself.

---

# 9. Test the setup with Cline

Before asking Cline to implement your application, give it a read-only test.

Ask:

```text
Inspect the local SaladCloud documentation under docs/salad/.

Do not modify any files.

Find the OpenAPI operation for creating a Container Group.

Use the local OpenAPI specification as the source of truth.

Report:

1. OpenAPI specification containing the operation
2. HTTP method
3. endpoint path
4. required path parameters
5. required headers
6. request body schema
7. required request fields
8. important enums
9. response schema
10. HTTP status codes

Do not use prior knowledge of SaladCloud.
Do not guess anything that cannot be verified from the local documentation.
```

Cline should inspect:

```text
docs/salad/api-specs/
```

and identify the appropriate operation.

---

# 10. Test actual code generation

Once the first test succeeds, ask Cline:

```text
Using only the local SaladCloud documentation, implement a typed client
function for creating a Container Group.

Before writing code:

1. Find the exact operation in the OpenAPI specification.
2. Verify the HTTP method.
3. Verify the endpoint.
4. Verify all required parameters.
5. Verify the request body.
6. Verify authentication.
7. Check the relevant Salad workflow documentation.

Do not invent fields or endpoints.

Do not use a Salad API key in the code.

Read SALAD_API_KEY from the environment.

After implementing the client, explain which OpenAPI operation was used.
```

This is a good test of whether Cline is actually using the local knowledge base.

---

# 11. Updating the Salad documentation

When you want to pull the latest Salad documentation:

```bash
./scripts/sync-salad-docs.sh
```

The script records the exact Git commit in:

```text
docs/salad/SOURCE_COMMIT
```

For example:

```text
Repository: https://github.com/SaladTechnologies/salad-cloud-docs.git
Commit: abc123...
Fetched: 2026-08-31T12:00:00Z
```

This makes it possible to know exactly which version of Salad's documentation Cline was using.

---

# 12. Recommended Git setup

I recommend committing the Cline rules and documentation to your project.

For example:

```bash
git add .clinerules/salad-api.md
git add docs/salad
git add scripts/sync-salad-docs.sh

git commit -m "Add SaladCloud API knowledge for Cline"
```

This gives the project a reproducible API knowledge base.

Another developer can clone the project and Cline will have the same Salad documentation revision.

---

# 13. Optional: don't commit the documentation

If you don't want the Salad documentation in Git, add:

```gitignore
docs/salad/
```

Then developers can run:

```bash
./scripts/sync-salad-docs.sh
```

after cloning the project.

For an application that depends heavily on Salad, however, committing the documentation revision is preferable because it makes API changes visible in Git diffs.

---

# 14. Recommended workflow

The resulting development workflow is:

```text
                    User request
                         │
                         ▼
                       Cline
                         │
                         ▼
              .clinerules/salad-api.md
                         │
                         ▼
              docs/salad/api-specs/
                         │
                  Exact API contract
                         │
                         ▼
              docs/salad/reference/
                         │
                API semantics/examples
                         │
                         ▼
          Product/workflow documentation
                         │
                         ▼
                  Generated code
                         │
                         ▼
                  SaladCloud API
```

The critical rule is:

```text
DO NOT GUESS THE SALAD API.
```

Cline should verify the API operation against the OpenAPI specification before writing the request.

---

# 15. Updating later

When you suspect Salad has changed its API:

```bash
./scripts/sync-salad-docs.sh
```

Then inspect:

```bash
git diff -- docs/salad
```

If you've committed the documentation, this gives you a straightforward view of changes between Salad API revisions.

---

# 16. Final installation checklist

From the project root:

```bash
mkdir -p .clinerules
mkdir -p docs/salad
mkdir -p scripts
```

Create:

```text
.clinerules/salad-api.md
scripts/sync-salad-docs.sh
```

Make the script executable:

```bash
chmod +x scripts/sync-salad-docs.sh
```

Synchronize Salad:

```bash
./scripts/sync-salad-docs.sh
```

Verify:

```bash
find docs/salad/api-specs -maxdepth 1 -type f -print | sort
```

Verify Cline rules:

```bash
find .clinerules -maxdepth 1 -type f -print
```

Expected:

```text
.clinerules/salad-api.md
```

You now have:

```text
.clinerules/salad-api.md
        +
docs/salad/api-specs/
        +
docs/salad/reference/
        +
docs/salad product documentation
        ↓
      Cline
        ↓
Correct SaladCloud API code
```

## Why this setup

This is preferable to scraping `docs.salad.com` because Salad's public documentation repository contains the source documentation and OpenAPI specifications directly. Salad's own repository documentation identifies the API specifications as OpenAPI 3.0, making them a much better source of truth for an AI coding agent than rendered HTML.

For Cline, the goal is not to "teach it everything about Salad." The goal is to give it **a reliable API contract and explicit instructions to verify its work against that contract**.
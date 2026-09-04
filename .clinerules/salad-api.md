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
    docs/salad/api-reference/
    docs/salad/guides/

---

## Source-of-truth hierarchy

Use sources in this order:

1. OpenAPI specifications in `docs/salad/api-specs/`
2. API reference in `docs/salad/api-reference/`
3. Salad workflow/how-to documentation in `docs/salad/guides/`
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

Authentication uses the Salad API key header:

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
2. Search `docs/salad/api-reference/`.
3. Search `docs/salad/guides/`.
4. If still unresolved, explicitly state what could not be verified.

Correctness is more important than producing an immediate API call.

## Preferred implementation pattern

When implementing SaladCloud API integrations in Python:

1. First determine whether the project already has a SaladCloud SDK.
2. If an official Salad SDK is appropriate and available, prefer it.
3. Otherwise use the OpenAPI specification to implement the HTTP request.
4. Keep the Salad API layer isolated from application/business logic.
5. Use typed request and response models where appropriate.
6. Never spread raw Salad API calls throughout the application.

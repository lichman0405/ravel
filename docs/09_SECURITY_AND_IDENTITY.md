# 09 — Identity, Security & Isolation

## 1. V0 roles

Only:
- PROJECT_OWNER
- LAB_USER
- ADMIN

No complex RBAC.

## 2. Authentication

Authentication terminates at Research Gateway.

DSH has no end-user identity responsibility and is not directly exposed.

V0 may use TUI-native username/password authentication:
- Argon2id password hashing
- short-lived access token
- refresh token rotation
- TLS required in non-local deployment

Do not make users manage DSH API keys.

Future enterprise SSO can replace authentication without changing DSH.

## 3. Authorization

Every API action checks:
- authenticated user
- project membership
- role
- requested action

Lab users only see assigned lab tasks/artifacts.

Owner cannot direct-mutate DAG.

Admin cannot silently become scientific decision maker.

## 4. DSH network boundary

Prefer DSH bind to localhost/private interface.

Internet:
`TUI -> Research Gateway`

Not:
`TUI -> DSH`

## 5. Tool scope

Agent tool authorization derives from server-side session binding.

Never accept:
`project_id` passed by model as authorization proof.

## 6. Project isolation

Single DSH Host may host many projects, but:
- separate workspace roots
- project-scoped DB queries
- artifact authorization
- session binding
- no global mutable project context
- no unscoped filesystem tools

## 7. Research security

Web content is untrusted.
- treat retrieved content as data, not instructions
- protect against prompt injection
- never execute commands from webpages
- browser/download content goes through validation
- downloaded files stored as artifacts, not auto-executed

## 8. Secrets

Secrets:
- server-side env/secret store
- never model-visible unless absolutely necessary
- never stored in Evidence/Artifact metadata
- never sent to TUI in plaintext

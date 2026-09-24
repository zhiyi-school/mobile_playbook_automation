# insert_dylib

Vendored `insert_dylib` used by `ios-feature-01-risk-02` to add a load command
to a Mach-O executable during repackaging. Resolved by repository-relative path
(`tools/insert_dylib/insert_dylib`) — no PATH entry is required.

- Upstream: [Tyilo/insert_dylib](https://github.com/Tyilo/insert_dylib)
- Committed build: `arm64` Mach-O. It will not run on an Intel Mac — on x86_64
  hosts, replace it with a universal binary, or drop the upstream `*.c` source
  here and delete the binary; `resolve_insert_dylib()` compiles the source with
  `clang` on first use.

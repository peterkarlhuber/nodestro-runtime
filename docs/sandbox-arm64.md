# Built-in Linux sandbox on AArch64 (Linux ARM64)

Status: decision record, 2026-11-02. Scope: `nodestro_runtime.builtin_sandbox.LinuxSandbox` and
`nodestro_runtime.sandbox_confine` (Landlock ABI >= 4 + seccomp BPF + rlimits) on Linux AArch64.

## Question

Can the existing `LinuxSandbox` be used on Linux ARM64 (for example inside an arm64 Linux guest on Apple Silicon),
and does the x86-64 security evidence (probe runs on Ubuntu 24.04 and Debian 13) carry over? Does anything fail open
on AArch64 today?

## Answer

No, the x86-64 evidence does not carry over, and the sandbox is not usable on AArch64 today. Nothing fails open:
on AArch64 the sandbox is refused by three independent checks before any tool code runs. Untrusted code on Linux
ARM64 therefore needs Docker (the `auto` mode picks it when it runs), exactly as on any other untested system.

## What differs between x86-64 and AArch64

| Part | x86-64 | AArch64 | Effect on the sandbox |
|---|---|---|---|
| seccomp `arch` value | `AUDIT_ARCH_X86_64` `0xC000003E` | `AUDIT_ARCH_AARCH64` `0xC00000B7` | The filter accepts only `0xC000003E`; every other value is `KILL_PROCESS`. |
| System call table | `asm/unistd_64.h` | generic `asm-generic/unistd.h` | Numbers below 424 differ for almost every call. |
| Legacy calls | `fork` 57, `vfork` 58, `open`, `dup2`, ... | none (only `clone`, `openat`, `dup3`, ...) | Denying `fork`/`vfork` has no AArch64 counterpart; `clone` must carry the check. |
| Second ABI | x32 (bit `0x40000000`), i386 (`int 0x80`, other `arch`) | AArch32 only via `execve` of a 32-bit program | The x32 bit is killed; i386 and AArch32 fail the `arch` check. |
| `clone` argument order | `flags, stack, ptid, ctid, tls` | `flags, stack, ptid, tls, ctid` | The filter reads only `flags` (argument 0): the same on both. |
| Landlock calls | 444, 445, 446 | 444, 445, 446 | Same (unified numbers from 424 on). Structs are `u64` fields; `landlock_path_beneath_attr` is packed (12 bytes) on both. |
| `prctl`, `seccomp`, `TIOCSTI`/`TIOCLINUX`, `RLIMIT_*` | generic | generic | Same constants. |

Examples of the same number naming different calls: x86-64 `fork` 57 is AArch64 `close`, `execve` 59 is `pipe2`,
`ptrace` 101 is `nanosleep`, `socket` 41 is `pivot_root`, `clone` 56 is `openat`. On AArch64 the dangerous calls
are `clone` 220, `execve` 221, `ptrace` 117, `socket` 198, `process_vm_readv` 270, `bpf` 280, `perf_event_open`
241, `userfaultfd` 282, `keyctl` 219, `mount` 40, `unshare` 97, `personality` 92 (`io_uring_setup` 425 and `clone3`
435 are the same).

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| A1 | None (verified) | On AArch64 `LinuxSandbox.status()` reports `platform` (`builtin_sandbox._check` and `system_supported` require `x86_64`), so the policy never counts it as isolating and `auto` never selects it. | Regression test + CI job |
| A2 | None (verified) | The confinement script refuses on its own (`_check_platform`: `needs Linux x86-64`, exit 97) even if a caller builds a `LinuxSandbox` and skips the status check. | Regression test + CI job runs the real script on arm64 |
| A3 | None (verified) | The seccomp filter kills every system call whose `arch` is not x86-64 (AArch64, AArch32, i386, big-endian AArch64, RISC-V, ppc64le, s390x) and every x86-64 number with the x32 bit or above. It never returns `ALLOW` for an unknown architecture. | BPF interpreter test over numbers 0-1099 and edge values |
| A4 | Critical if it happened (latent) | Enabling AArch64 by relaxing only the platform checks would give a filter that kills every call (fail closed but broken); additionally switching only the `arch` constant would deny the wrong calls (`close`, `pipe2`, `nanosleep`, ...) and leave `clone`, `execve`, `ptrace`, `socket`, `bpf`, `process_vm_readv` allowed. | Prevented: tests pin the x86-64 numbers to the kernel headers and the filter to x86-64; any AArch64 support needs its own table and evidence (below). |
| A5 | Medium (latent, evidence) | The probes call raw system calls by x86-64 number (`keyctl` 250, `clone` 56, `ptrace` 101, `shmget` 29, `process_vm_readv` 310, `bpf` 321, `perf_event_open` 298, `userfaultfd` 323, `mount` 165, `setns` 308, `open_by_handle_at` 304). On AArch64 these name other calls or return `ENOSYS`, which a probe would record as "refused": false evidence. | Fixed: `sandbox_probes.run_all` refuses to run on any architecture other than x86-64. |
| A6 | Low | The filter is a deny list (default `ALLOW`); calls added after the list was written (for example `quotactl_fd` 443, `statmount` 457, `listmount` 458) are allowed on x86-64. Landlock, `no_new_privs`, `RLIMIT_NPROC=0` and missing capabilities still apply. | Not changed (x86-64 behavior kept identical); follow-up in RT-018. |
| A7 | Unverified | An x86-64 interpreter under emulation (Rosetta for Linux, QEMU user mode) in an AArch64 guest: if it reports `x86_64`, the kernel still sees AArch64 system calls, which the filter kills, so the sandbox fails its self-test (`failed`). Not tested. | Fails closed by construction; no test run. |
| A8 | None (verified on x86-64) | `/proc/<parent>/environ`, `/proc/<parent>/cmdline`, `/proc/<parent>/fd` and `/proc/<parent>/mem` are not readable: Landlock grants no rule for `/proc` (probe `environment-and-keys`). Not applicable on AArch64 while the sandbox is unavailable there. | Probe passes (local run and CI x86-64 job) |

## What changed

- `sandbox_probes.run_all` refuses to run on non-x86-64 machines (A5).
- `tests/test_sandbox_arch.py`: a classic-BPF interpreter evaluates the real filter for every number 0-1099 and edge
  values under seven foreign `arch` values (all `KILL`) and under x86-64 (deny list `EPERM`, `clone3` `ENOSYS`,
  `socket` `EAFNOSUPPORT`, process-like `clone` refused, thread `clone` allowed, x32 `KILL`); the x86-64 numbers
  (deny list, checked calls, Landlock) are compared with the machine's `asm/unistd_64.h`; AArch64 is refused by the
  host check, the confinement check and the probes; on a real non-x86-64 machine the real status is `platform`
  and the real confinement script exits 97 before its runner runs.
- CI job `builtin-sandbox-arm64` on `ubuntu-24.04-arm` (real arm64 runner) runs these tests and checks that the probe
  command exits with status 2 and reason `platform`. The `builtin-sandbox` job on `ubuntu-24.04` (x86-64, with
  `NODESTRO_REQUIRE_SANDBOX=1`) is unchanged.
- The confinement code (`sandbox_confine.py`) and x86-64 behavior are unchanged.

Test runs: see the CI runs of branch `claude/project-thread-qz41p9` (commit message and pull request). Locally
(x86-64 container, kernel 6.18, Landlock ABI 7, normal user): the probe command passed 31 of 31 checks;
`tests/test_builtin_sandbox.py`, `tests/test_sandbox_arch.py` and `tests/test_sandbox_policy.py` with
`NODESTRO_REQUIRE_SANDBOX=1` passed.

## Decision

Keep `LinuxSandbox` x86-64 only and failing closed on AArch64. The macOS VM plan that would need it inside an arm64
guest is paused (Docker first), so no AArch64 support is added now.

If AArch64 is needed later, extend `LinuxSandbox` rather than choosing another isolation: Landlock and seccomp are
architecture-neutral apart from the `arch` constant and the system call numbers, and inside a VM guest the same
argument against user namespaces (AppArmor on Ubuntu) holds or the guest is ours to configure. The extension needs:
a per-architecture table (AArch64 numbers from `asm-generic/unistd.h`, no `fork`/`vfork`/`uselib`/`iopl`/`ioperm`)
with the filter built only for the running architecture and `KILL` for any other; a check that the interpreter is
a native 64-bit little-endian program (A7); probes that take their numbers per architecture and require `EPERM`
(not merely an error, since `ENOSYS` from a wrong number would look like a refusal); a test against the machine's
headers on both runners; and a probe run on `ubuntu-24.04-arm` with `NODESTRO_REQUIRE_SANDBOX=1`, plus a run in the
actual guest image, before any AArch64 system joins the tested systems.

#!/usr/bin/env -S node --import tsx
// Docker 既定 seccomp profile (moby/profiles) を読み、rootless Podman 用に必要な syscall だけを足した
// profile を stdout に出す。書込/差分検査は Makefile (gen-seccomp / check-seccomp) が担う。
// 出典 default.json は http-cache/ に pin (HTTP_CACHE_REFRESH=1 で再取得)。開く/開かない syscall の
// 根拠と運用は .devcontainer/core/PODMAN.md「seccomp profile (tightening)」。
import { deepEqual } from "./lib/deepEqual.ts";
import { cachedFetch } from "./lib/httpCache.ts";

const DEFAULT_PROFILE_URL = "https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json";

interface SeccompArg {
  index: number;
  value: number;
  valueTwo?: number;
  op: string;
}
interface SyscallRule {
  names: string[];
  action: string;
  args?: SeccompArg[];
  errnoRet?: number;
  comment?: string;
  includes?: { caps?: string[]; arches?: string[] };
  excludes?: { caps?: string[]; arches?: string[] };
}
interface SeccompProfile {
  defaultAction: string;
  syscalls: SyscallRule[];
  [key: string]: unknown;
}

function only(rules: SyscallRule[], what: string): SyscallRule {
  if (rules.length !== 1) throw new Error(`expected exactly 1 ${what}, found ${rules.length}`);
  return rules[0];
}

function tailor(profile: SeccompProfile): SeccompProfile {
  const sc = profile.syscalls;

  // 既定は非 CAP_SYS_ADMIN だと clone の CLONE_NEW* (arg0 の mask) を禁じる → arg 制限を外す
  // (s390 用の別 group は includes arches で見分けて触らない)。
  const clone = only(
    sc.filter(
      (g) =>
        deepEqual(g.names, ["clone"]) &&
        g.action === "SCMP_ACT_ALLOW" &&
        g.includes === undefined &&
        g.args?.some((a) => a.index === 0) &&
        deepEqual(g.excludes?.caps, ["CAP_SYS_ADMIN"]),
    ),
    "clone arg-restricted group (non-CAP_SYS_ADMIN)",
  );
  clone.args = undefined;
  clone.comment = "rootless podman (dev): allow clone with CLONE_NEW* (userns/mount/net ns creation)";

  // 既定は非 CAP_SYS_ADMIN だと clone3 を ENOSYS にする → 許可。
  const clone3 = only(
    sc.filter((g) => deepEqual(g.names, ["clone3"]) && g.action === "SCMP_ACT_ERRNO"),
    "clone3 ERRNO group (non-CAP_SYS_ADMIN)",
  );
  clone3.action = "SCMP_ACT_ALLOW";
  clone3.errnoRet = undefined;
  clone3.comment = "rootless podman (dev): allow clone3 (default returns ENOSYS without CAP_SYS_ADMIN)";

  // dev が持たない CAP_SYS_CHROOT の gate を外し chroot を常時許可する (syscall を通すだけ。実権限は
  // kernel の cap チェックが握る。根拠と安全性は PODMAN.md「seccomp profile (tightening)」)。
  const chroot = only(
    sc.filter(
      (g) =>
        deepEqual(g.names, ["chroot"]) &&
        g.action === "SCMP_ACT_ALLOW" &&
        deepEqual(g.includes?.caps, ["CAP_SYS_CHROOT"]),
    ),
    "chroot CAP_SYS_CHROOT-gated group",
  );
  chroot.includes = undefined;
  chroot.comment = "rootless podman (dev): allow chroot (default gates it behind CAP_SYS_CHROOT, which dev lacks)";

  // CAP_SYS_ADMIN gate されている namespace/mount 系のうち rootless に要る分だけを開く
  // (pivot_root/keyctl は既定 profile に無く defaultAction で塞がれる)。開く/開かない一覧は PODMAN.md。
  sc.push({
    names: [
      "unshare",
      "setns",
      "mount",
      "umount",
      "umount2",
      "pivot_root",
      "mount_setattr",
      "fsopen",
      "fsconfig",
      "fsmount",
      "fspick",
      "move_mount",
      "open_tree",
      "sethostname",
      "setdomainname",
      "keyctl",
    ],
    action: "SCMP_ACT_ALLOW",
    excludes: { caps: ["CAP_SYS_ADMIN"] },
    comment: "rootless podman (dev): namespace/mount setup syscalls, opened only to non-CAP_SYS_ADMIN containers",
  });

  return profile;
}

const profile = tailor(JSON.parse(await cachedFetch(DEFAULT_PROFILE_URL)) as SeccompProfile);
process.stdout.write(`${JSON.stringify(profile, null, 2)}\n`);

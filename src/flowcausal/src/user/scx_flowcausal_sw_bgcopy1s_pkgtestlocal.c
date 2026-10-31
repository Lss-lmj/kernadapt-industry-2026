// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal-StageWidth-BgCopy1s-PackageTestLocal.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal_sw_bgcopy1s_pkgtestlocal.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal_sw_bgcopy1s_pkgtestlocal
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal_sw_bgcopy1s_pkgtestlocal"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal_sw_bgcopy1s_pkgtestlocal_ops

#include "scx_flowcausal.c"

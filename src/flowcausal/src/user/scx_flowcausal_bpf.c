// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal-BPF.
 *
 * This variant keeps the classic FlowCausal scheduling core, but defaults to
 * in-kernel automatic frontier promotion and uses its own bpffs namespace.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal_bpf.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal_bpf
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal_bpf"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 0
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal_bpf_ops

#include "scx_flowcausal.c"

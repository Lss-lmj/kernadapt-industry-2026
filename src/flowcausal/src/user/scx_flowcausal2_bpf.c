// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal2-BPF.
 *
 * This is the behavior-mode-aware pure-BPF line: the kernel auto-classifies
 * tracked tasks, then routes interactive / compute / tail / background work
 * with FlowCausal2's existing scheduler policy.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal2_bpf.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal2_bpf
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal2_bpf"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal2_bpf_ops

#include "scx_flowcausal.c"

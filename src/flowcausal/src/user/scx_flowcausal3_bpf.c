// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal3-BPF.
 *
 * FlowCausal3 is the BPF-main/C-assist performance line.  It defaults to the
 * same in-kernel automatic frontier promotion as FlowCausal-BPF, but its BPF
 * program emits only exec events for the C assist path.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal3_bpf.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal3_bpf
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal3_bpf"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 0
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal3_bpf_ops

#include "scx_flowcausal.c"

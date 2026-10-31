// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal-StageWidth-CxxBudget20.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal_stagewidth_cxx_budget20.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal_stagewidth_cxx_budget20
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal_stagewidth_cxx_budget20"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal_stagewidth_cxx_budget20_ops

#include "scx_flowcausal.c"

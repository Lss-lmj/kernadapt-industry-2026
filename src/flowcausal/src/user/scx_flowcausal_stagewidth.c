// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal-StageWidth.
 *
 * This is the no-converge width-control branch: BPF owns the scheduler logic,
 * and userspace only loads/pins the object.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal_stagewidth.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal_stagewidth
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal_stagewidth"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal_stagewidth_ops

#include "scx_flowcausal.c"

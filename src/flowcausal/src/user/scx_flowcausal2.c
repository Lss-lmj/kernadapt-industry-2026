// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for FlowCausal2.
 *
 * FlowCausal2 keeps the same operational interface as scx_flowcausal, but
 * uses a separate BPF skeleton, separate default bpffs pin directory, and
 * enables behavior-mode-aware routing by default.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal2.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal2
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal2"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1

#define flowcausal_ops flowcausal2_ops

#include "scx_flowcausal.c"

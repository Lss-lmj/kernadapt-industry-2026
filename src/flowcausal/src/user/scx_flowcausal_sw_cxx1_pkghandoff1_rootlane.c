// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for
 * FlowCausal-StageWidth-Cxx1-PackageHandoff1-RootLane.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal_sw_cxx1_pkghandoff1_rootlane.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal_sw_cxx1_pkghandoff1_rootlane
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal_sw_cxx1_pkghandoff1_rootlane"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal_sw_cxx1_pkghandoff1_rootlane_ops

#include "scx_flowcausal.c"

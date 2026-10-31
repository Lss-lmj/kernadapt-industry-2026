// SPDX-License-Identifier: GPL-2.0
/*
 * Userspace loader for
 * FlowCausal-StageWidth-PCpu0-PackageHandoff-ParentSlice1ms.
 */
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal_sw_pccpu0_pkghandoff_parentslice1ms.bpf.skel.h"
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal_sw_pccpu0_pkghandoff_parentslice1ms
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal_sw_pccpu0_pkghandoff_parentslice1ms"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 1
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 1

#define flowcausal_ops flowcausal_sw_pccpu0_pkghandoff_parentslice1ms_ops

#include "scx_flowcausal.c"

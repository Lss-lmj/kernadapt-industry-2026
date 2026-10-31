// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal2-BPF: behavior-mode-aware FlowCausal2 with in-kernel automatic
 * workflow classification.
 *
 * Compared with scx_flowcausal_bpf, this variant also enables FlowCausal2's
 * interactive/compute/tail/background mode routing by default.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal2_bpf"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE true
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER true

#define flowcausal_ops flowcausal2_bpf_ops
#define flowcausal_select_cpu flowcausal2_bpf_select_cpu
#define flowcausal_enqueue flowcausal2_bpf_enqueue
#define flowcausal_dispatch flowcausal2_bpf_dispatch
#define flowcausal_running flowcausal2_bpf_running
#define flowcausal_stopping flowcausal2_bpf_stopping
#define flowcausal_enable flowcausal2_bpf_enable
#define flowcausal_init flowcausal2_bpf_init
#define flowcausal_exit flowcausal2_bpf_exit
#define flowcausal_on_fork flowcausal2_bpf_on_fork
#define flowcausal_on_exec flowcausal2_bpf_on_exec
#define flowcausal_on_exit flowcausal2_bpf_on_exit
#define flowcausal_on_free flowcausal2_bpf_on_free
#define flowcausal_on_wakeup flowcausal2_bpf_on_wakeup
#define flowcausal_on_wakeup_new flowcausal2_bpf_on_wakeup_new

#include "scx_flowcausal.bpf.c"

// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal3-BPF: BPF-main/C-assist optimized FlowCausal.
 *
 * The scheduling policy stays close to FlowCausal-BPF, but the control-plane
 * event stream is narrowed to exec events.  The C assist only needs exec to
 * read command lines and refine BPF's comm-based classification; fork/wakeup,
 * switch, fallback, and cleanup events remain internal scheduler accounting.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal3_bpf"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE false
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER true
#define FLOWCAUSAL_DEFAULT_CONTROL_EVENT_MASK (1U << FLOW_EVT_EXEC)
#define FLOWCAUSAL_DEFAULT_RESPECT_C_ASSIST true

#define flowcausal_ops flowcausal3_bpf_ops
#define flowcausal_select_cpu flowcausal3_bpf_select_cpu
#define flowcausal_enqueue flowcausal3_bpf_enqueue
#define flowcausal_dispatch flowcausal3_bpf_dispatch
#define flowcausal_running flowcausal3_bpf_running
#define flowcausal_stopping flowcausal3_bpf_stopping
#define flowcausal_enable flowcausal3_bpf_enable
#define flowcausal_init flowcausal3_bpf_init
#define flowcausal_exit flowcausal3_bpf_exit
#define flowcausal_on_fork flowcausal3_bpf_on_fork
#define flowcausal_on_exec flowcausal3_bpf_on_exec
#define flowcausal_on_exit flowcausal3_bpf_on_exit
#define flowcausal_on_free flowcausal3_bpf_on_free
#define flowcausal_on_wakeup flowcausal3_bpf_on_wakeup
#define flowcausal_on_wakeup_new flowcausal3_bpf_on_wakeup_new

#include "scx_flowcausal.bpf.c"

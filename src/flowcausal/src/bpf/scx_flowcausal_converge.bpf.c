// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-Converge: execution-flow convergence scheduler.
 *
 * This variant keeps the no-Python online path.  BPF owns fork/exec/enqueue
 * state, detects whether a tracked harness session is in a wide phase or a
 * tail/convergence phase, routes tail work to a dedicated convergence DSQ, and
 * routes ordinary frontier work through per-session frontier DSQ slots.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_converge"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE true
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER true
#define FLOWCAUSAL_DEFAULT_CONTROL_EVENT_MASK (1U << FLOW_EVT_EXEC)
#define FLOWCAUSAL_DEFAULT_RESPECT_C_ASSIST true
#define FLOWCAUSAL_DEFAULT_CONVERGE true
#define FLOWCAUSAL_DEFAULT_SESSION_FRONTIER_DSQ true
#define FLOWCAUSAL_DEFAULT_ADAPTIVE_WIDTH true
#define FLOWCAUSAL_DEFAULT_CONVERGE_TAIL_DSQ true

#define flowcausal_ops flowcausal_converge_ops
#define flowcausal_select_cpu flowcausal_converge_select_cpu
#define flowcausal_enqueue flowcausal_converge_enqueue
#define flowcausal_dispatch flowcausal_converge_dispatch
#define flowcausal_running flowcausal_converge_running
#define flowcausal_stopping flowcausal_converge_stopping
#define flowcausal_enable flowcausal_converge_enable
#define flowcausal_init flowcausal_converge_init
#define flowcausal_exit flowcausal_converge_exit
#define flowcausal_on_fork flowcausal_converge_on_fork
#define flowcausal_on_exec flowcausal_converge_on_exec
#define flowcausal_on_exit flowcausal_converge_on_exit
#define flowcausal_on_free flowcausal_converge_on_free
#define flowcausal_on_wakeup flowcausal_converge_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_converge_on_wakeup_new

#include "scx_flowcausal.bpf.c"

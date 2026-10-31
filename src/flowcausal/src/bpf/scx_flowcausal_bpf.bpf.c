// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-BPF: original FlowCausal frontier mechanism with in-kernel
 * automatic workflow classification.
 *
 * Userspace only registers a sandbox/session root.  BPF tracks fork/exec
 * descendants, classifies compute-like children from comm/runtime/wait
 * evidence, and turns those children into bounded frontier candidates.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_bpf"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE false
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER true

#define flowcausal_ops flowcausal_bpf_ops
#define flowcausal_select_cpu flowcausal_bpf_select_cpu
#define flowcausal_enqueue flowcausal_bpf_enqueue
#define flowcausal_dispatch flowcausal_bpf_dispatch
#define flowcausal_running flowcausal_bpf_running
#define flowcausal_stopping flowcausal_bpf_stopping
#define flowcausal_enable flowcausal_bpf_enable
#define flowcausal_init flowcausal_bpf_init
#define flowcausal_exit flowcausal_bpf_exit
#define flowcausal_on_fork flowcausal_bpf_on_fork
#define flowcausal_on_exec flowcausal_bpf_on_exec
#define flowcausal_on_exit flowcausal_bpf_on_exit
#define flowcausal_on_free flowcausal_bpf_on_free
#define flowcausal_on_wakeup flowcausal_bpf_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_bpf_on_wakeup_new

#include "scx_flowcausal.bpf.c"

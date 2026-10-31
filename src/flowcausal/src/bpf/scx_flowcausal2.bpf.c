// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal2: behavior-mode-aware variant of scx_flowcausal.
 *
 * This file deliberately reuses the FlowCausal datapath and changes only the
 * compiled scheduler identity and default policy preset.  FlowCausal2 is the
 * experimental line where workflow-internal behavior modes become first-class:
 * interactive, compute, tail, and background.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal2"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE true

#define flowcausal_ops flowcausal2_ops
#define flowcausal_select_cpu flowcausal2_select_cpu
#define flowcausal_enqueue flowcausal2_enqueue
#define flowcausal_dispatch flowcausal2_dispatch
#define flowcausal_running flowcausal2_running
#define flowcausal_stopping flowcausal2_stopping
#define flowcausal_enable flowcausal2_enable
#define flowcausal_init flowcausal2_init
#define flowcausal_exit flowcausal2_exit
#define flowcausal_on_fork flowcausal2_on_fork
#define flowcausal_on_exec flowcausal2_on_exec
#define flowcausal_on_exit flowcausal2_on_exit
#define flowcausal_on_free flowcausal2_on_free
#define flowcausal_on_wakeup flowcausal2_on_wakeup
#define flowcausal_on_wakeup_new flowcausal2_on_wakeup_new

#include "scx_flowcausal.bpf.c"

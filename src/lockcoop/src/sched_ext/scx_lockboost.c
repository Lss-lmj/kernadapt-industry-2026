/* SPDX-License-Identifier: GPL-2.0 */
/* LockBoost loader: LockCoop observation with only owner progress enabled. */
#define LOCKCOOP_SKEL_HEADER "scx_lockboost.bpf.skel.h"
#define LOCKCOOP_SKEL scx_lockboost
#define LOCKCOOP_OPS lockboost_ops
#define LOCKCOOP_DESTROY scx_lockboost__destroy
#define LOCKCOOP_SKEL__open scx_lockboost__open
#define LOCKCOOP_SKEL__load scx_lockboost__load
#define LOCKCOOP_SKEL__attach scx_lockboost__attach
#define LOCKCOOP_SCHED_LABEL "scx_lockboost"
#define LOCKCOOP_FORCE_NEST 0
#define LOCKCOOP_FORCE_BOOST 1
#include "scx_lockcoop.c"

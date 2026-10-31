KERNEL_SRC ?= /lib/modules/$(shell uname -r)/build
SCHED_EXT ?= $(KERNEL_SRC)/tools/sched_ext
MACHINE := $(shell uname -m)
TARGET_ARCH ?= $(if $(filter aarch64 arm64,$(MACHINE)),arm64,$(if $(filter riscv64,$(MACHINE)),riscv,x86))
MULTIARCH := $(shell $(CC) -dumpmachine)
SCX_BUILD ?= $(SCHED_EXT)/build
CLANG ?= clang
BPF_OPT ?= -O2
CC ?= gcc
BPFTOOL ?= bpftool
TMPDIR ?= /tmp

BPF_CFLAGS := -g $(BPF_OPT) -mcpu=v3 -target bpf -D__TARGET_ARCH_$(TARGET_ARCH) \
	-Iinclude \
	-I$(SCHED_EXT)/include \
	-I$(SCX_BUILD)/include \
	-I$(KERNEL_SRC)/tools/include/uapi \
	-I$(KERNEL_SRC)/tools/include \
	-I$(KERNEL_SRC)/tools/lib \
	-I$(KERNEL_SRC)/include \
	-I/usr/include/$(MULTIARCH) \
	-Wall -Wno-compare-distinct-pointer-types

USER_CFLAGS := -g -O2 -Wall -Werror \
	-Iinclude \
	-I$(TMPDIR) \
	-I$(SCX_BUILD)/include \
	-I$(SCHED_EXT)/include \
	-I$(KERNEL_SRC)/tools/lib \
	-I$(KERNEL_SRC)/tools/include \
	-I$(KERNEL_SRC)/tools/include/uapi

USER_LDLIBS := $(shell pkg-config --libs libbpf 2>/dev/null || echo -lbpf)
LOADER_LDLIBS := $(SCX_BUILD)/obj/libbpf/libbpf.a -lelf -lz -lpthread

.PHONY: check-cpu-workload-v2 check-normal-workload-v2 check-pipe-workload-v2 test-background-v2 test-normal-v2 test-event-client-v2 test-nested-wait-v2 test-sleeping-descendant-v2 test-feedback

.PHONY: check check-all check-flowcausal-v2 check-flowcausal-feedback check-bpf-v2 check-bpf-feedback check-loader-v2 check-loader-feedback check-daemon-v2 check-daemon-feedback check-event-client-v2 check-test-helper-v2 check-latency-workload-v2 test-lifecycle-v2 test-roles-v2 test-protocol-v2 test-parallel-width-v2 test-capacity-v2 test-resources-v2 test-dynamic-weight-v2 test-automatic-weight-v2 test-quota-v2 test-latency-v2 check-bpf check-bpf2 check-bpf-auto check-bpf2-auto check-bpf3-auto check-bpf-converge check-bpf-stagewidth check-bpf-stagewidth-chainboost check-bpf-stagewidth-chainlocal check-bpf-stagewidth-cxxfirst500 check-bpf-stagewidth-cxxspan2 check-bpf-stagewidth-cxxfirst500-cpu7 check-bpf-stagewidth-packdelay500-cpu7 check-bpf-stagewidth-cxx-unbudget check-bpf-stagewidth-cxx-budget20 check-bpf-stagewidth-parentboost check-bpf-stagewidth-parentboost200 check-bpf-stagewidth-parentlatency check-bpf-stagewidth-parentlatency-dfl check-bpf-stagewidth-parentcleanup check-bpf-stagewidth-parentcleanup-cpu0 check-bpf-stagewidth-parentcleanup-nopack-cpu0 check-bpf-stagewidth-parentcleanup-cxxonly-cpu0 check-bpf-stagewidth-parentcleanup-cxxonly-cpu0-200ms check-bpf-stagewidth-spanidle check-user check-loader check-loader2 check-loader-auto check-loader2-auto check-loader3-auto check-loader-converge check-loader-stagewidth check-loader-stagewidth-chainboost check-loader-stagewidth-chainlocal check-loader-stagewidth-cxxfirst500 check-loader-stagewidth-cxxspan2 check-loader-stagewidth-cxxfirst500-cpu7 check-loader-stagewidth-packdelay500-cpu7 check-loader-stagewidth-cxx-unbudget check-loader-stagewidth-cxx-budget20 check-loader-stagewidth-parentboost check-loader-stagewidth-parentboost200 check-loader-stagewidth-parentlatency check-loader-stagewidth-parentlatency-dfl check-loader-stagewidth-parentcleanup check-loader-stagewidth-parentcleanup-cpu0 check-loader-stagewidth-parentcleanup-nopack-cpu0 check-loader-stagewidth-parentcleanup-cxxonly-cpu0 check-loader-stagewidth-parentcleanup-cxxonly-cpu0-200ms check-loader-stagewidth-spanidle check-flowcausal2 check-flowcausal-bpf check-flowcausal2-bpf check-flowcausal3-bpf check-flowcausal-converge check-flowcausal-stagewidth check-flowcausal-stagewidth-chainboost check-flowcausal-stagewidth-chainlocal check-flowcausal-stagewidth-cxxfirst500 check-flowcausal-stagewidth-cxxspan2 check-flowcausal-stagewidth-cxxfirst500-cpu7 check-flowcausal-stagewidth-packdelay500-cpu7 check-flowcausal-stagewidth-cxx-unbudget check-flowcausal-stagewidth-cxx-budget20 check-flowcausal-stagewidth-parentboost check-flowcausal-stagewidth-parentboost200 check-flowcausal-stagewidth-parentlatency check-flowcausal-stagewidth-parentlatency-dfl check-flowcausal-stagewidth-parentcleanup check-flowcausal-stagewidth-parentcleanup-cpu0 check-flowcausal-stagewidth-parentcleanup-nopack-cpu0 check-flowcausal-stagewidth-parentcleanup-cxxonly-cpu0 check-flowcausal-stagewidth-parentcleanup-cxxonly-cpu0-200ms check-flowcausal-stagewidth-spanidle check-monitor check-daemon test test-controller test-matrix stage patch-sched-ext dry-run clean-stage

stage:
	cp bpf/scx_flowcausal.bpf.c $(SCHED_EXT)/scx_flowcausal.bpf.c
	cp user/scx_flowcausal.c $(SCHED_EXT)/scx_flowcausal.c
	cp include/flowcausal.h $(SCHED_EXT)/include/flowcausal.h

check: check-flowcausal-v2 check-user check-loader check-flowcausal2 check-flowcausal-bpf check-flowcausal2-bpf check-flowcausal3-bpf check-flowcausal-converge check-monitor check-daemon

check-bpf-v2:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_v2.bpf.c -o $(TMPDIR)/scx_flowcausal_v2.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_v2.bpf.o name scx_flowcausal_v2 > $(TMPDIR)/scx_flowcausal_v2.bpf.skel.h

check-loader-v2: check-bpf-v2
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_v2.c -o $(TMPDIR)/scx_flowcausal_v2 $(LOADER_LDLIBS)

check-daemon-v2:
	$(CC) $(USER_CFLAGS) user/flowd_v2.c -o $(TMPDIR)/flowd_v2 $(USER_LDLIBS)

.PHONY: test-active-tools-v2
test-active-tools-v2:
	$(CC) $(USER_CFLAGS) tests/flowcausal_v2_active_tools_test.c \
		-Wl,--wrap=bpf_map_get_next_key -Wl,--wrap=bpf_map_lookup_elem \
		-Wl,--wrap=bpf_map_update_elem -Wl,--wrap=bpf_map_delete_elem \
		-o $(TMPDIR)/flowcausal_v2_active_tools_test $(USER_LDLIBS)
	$(TMPDIR)/flowcausal_v2_active_tools_test

check-event-client-v2:
	$(CC) -O2 -Wall -Werror user/flowevent_v2.c -o $(TMPDIR)/flowevent_v2
	$(CC) -static -O2 -Wall -Werror user/flowevent_v2.c -o $(TMPDIR)/flowevent_v2-static

check-test-helper-v2:
	$(CC) $(USER_CFLAGS) tests/flowcausal_v2_test_helper.c -o $(TMPDIR)/flowcausal_v2_test_helper $(USER_LDLIBS)

check-latency-workload-v2:
	$(CC) -O2 -Wall -Werror tests/flowcausal_v2_latency_workload.c -o $(TMPDIR)/flowcausal_v2_latency_workload

check-cpu-workload-v2:
	$(CC) -O2 -Wall -Werror tests/flowcausal_v2_cpu_workload.c -o $(TMPDIR)/flowcausal_v2_cpu_workload

check-normal-workload-v2:
	$(CC) -O2 -Wall -Werror tests/flowcausal_v2_normal_workload.c -o $(TMPDIR)/flowcausal_v2_normal_workload

check-pipe-workload-v2:
	$(CC) -O2 -Wall -Werror tests/flowcausal_v2_pipe_workload.c -o $(TMPDIR)/flowcausal_v2_pipe_workload

test-lifecycle-v2: check-flowcausal-v2 check-test-helper-v2
	python3 ../experiments/test_flowcausal_v2_lifecycle.py

test-roles-v2: check-flowcausal-v2
	python3 ../experiments/test_flowcausal_v2_roles.py

test-protocol-v2: check-flowcausal-v2 check-test-helper-v2
	python3 ../experiments/test_flowcausal_v2_protocol.py

test-parallel-width-v2: check-flowcausal-v2 check-test-helper-v2
	python3 ../experiments/test_flowcausal_v2_parallel_width.py

test-capacity-v2: check-flowcausal-v2 check-test-helper-v2
	python3 ../experiments/test_flowcausal_v2_capacity.py

test-resources-v2: check-flowcausal-v2
	python3 ../experiments/test_flowcausal_v2_resources.py

test-dynamic-weight-v2: check-flowcausal-v2
	python3 ../experiments/test_flowcausal_v2_dynamic_weight.py

test-automatic-weight-v2: check-flowcausal-v2
	python3 ../experiments/test_flowcausal_v2_automatic_weight.py

test-nested-wait-v2: check-flowcausal-v2
	python3 ../experiments/test_flowcausal_v2_nested_wait.py

test-sleeping-descendant-v2: check-flowcausal-v2 check-pipe-workload-v2
	python3 ../experiments/test_flowcausal_v2_sleeping_descendant.py

test-quota-v2: check-flowcausal-v2 check-test-helper-v2
	python3 ../experiments/test_flowcausal_v2_quota.py

test-latency-v2: check-flowcausal-v2 check-latency-workload-v2
	python3 ../experiments/test_flowcausal_v2_latency.py

test-background-v2: check-flowcausal-v2 check-cpu-workload-v2
	python3 ../experiments/test_flowcausal_v2_background.py

test-normal-v2: check-flowcausal-v2 check-normal-workload-v2 check-latency-workload-v2
	python3 ../experiments/test_flowcausal_v2_normal.py

test-event-client-v2: check-event-client-v2
	python3 ../experiments/test_flowevent_v2.py

check-flowcausal-v2: check-loader-v2 check-daemon-v2 check-event-client-v2

check-bpf-feedback:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_feedback.bpf.c -o $(TMPDIR)/scx_flowcausal_feedback.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_feedback.bpf.o name scx_flowcausal_feedback > $(TMPDIR)/scx_flowcausal_feedback.bpf.skel.h

check-loader-feedback: check-bpf-feedback
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_feedback.c -o $(TMPDIR)/scx_flowcausal_feedback $(LOADER_LDLIBS)

check-daemon-feedback:
	$(CC) $(USER_CFLAGS) user/flowd_feedback.c -o $(TMPDIR)/flowd_feedback $(USER_LDLIBS)

check-flowcausal-feedback: check-loader-feedback check-daemon-feedback check-event-client-v2

test-feedback: check-flowcausal-feedback
	python3 ../experiments/test_flowcausal_feedback.py

check-all: check test

test: test-controller test-matrix

test-controller:
	cd controller && python3 -m unittest test_flowctl

test-matrix:
	python3 ../experiments/test_buildtest_matrix.py
	python3 ../experiments/test_test16_agent_matrix.py
	python3 ../experiments/test_claim_gate_audit.py

check-bpf:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal.bpf.c -o $(TMPDIR)/scx_flowcausal.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal.bpf.o name scx_flowcausal > $(TMPDIR)/scx_flowcausal.bpf.skel.h

check-bpf2:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal2.bpf.c -o $(TMPDIR)/scx_flowcausal2.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal2.bpf.o name scx_flowcausal2 > $(TMPDIR)/scx_flowcausal2.bpf.skel.h

check-bpf-auto:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_bpf.bpf.c -o $(TMPDIR)/scx_flowcausal_bpf.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_bpf.bpf.o name scx_flowcausal_bpf > $(TMPDIR)/scx_flowcausal_bpf.bpf.skel.h

check-bpf2-auto:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal2_bpf.bpf.c -o $(TMPDIR)/scx_flowcausal2_bpf.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal2_bpf.bpf.o name scx_flowcausal2_bpf > $(TMPDIR)/scx_flowcausal2_bpf.bpf.skel.h

check-bpf3-auto:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal3_bpf.bpf.c -o $(TMPDIR)/scx_flowcausal3_bpf.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal3_bpf.bpf.o name scx_flowcausal3_bpf > $(TMPDIR)/scx_flowcausal3_bpf.bpf.skel.h

check-bpf-converge:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_converge.bpf.c -o $(TMPDIR)/scx_flowcausal_converge.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_converge.bpf.o name scx_flowcausal_converge > $(TMPDIR)/scx_flowcausal_converge.bpf.skel.h

check-bpf-stagewidth:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth.bpf.o name scx_flowcausal_stagewidth > $(TMPDIR)/scx_flowcausal_stagewidth.bpf.skel.h

check-bpf-stagewidth-chainboost:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_chainboost.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_chainboost.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_chainboost.bpf.o name scx_flowcausal_stagewidth_chainboost > $(TMPDIR)/scx_flowcausal_stagewidth_chainboost.bpf.skel.h

check-bpf-stagewidth-chainlocal:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_chainlocal.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_chainlocal.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_chainlocal.bpf.o name scx_flowcausal_stagewidth_chainlocal > $(TMPDIR)/scx_flowcausal_stagewidth_chainlocal.bpf.skel.h

check-bpf-stagewidth-cxxfirst500:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_cxxfirst500.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500.bpf.o name scx_flowcausal_stagewidth_cxxfirst500 > $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500.bpf.skel.h

check-bpf-stagewidth-cxxspan2:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_cxxspan2.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxxspan2.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_cxxspan2.bpf.o name scx_flowcausal_stagewidth_cxxspan2 > $(TMPDIR)/scx_flowcausal_stagewidth_cxxspan2.bpf.skel.h

check-bpf-stagewidth-cxxfirst500-cpu7:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_cxxfirst500_cpu7.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500_cpu7.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500_cpu7.bpf.o name scx_flowcausal_stagewidth_cxxfirst500_cpu7 > $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500_cpu7.bpf.skel.h

check-bpf-stagewidth-packdelay500-cpu7:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_packdelay500_cpu7.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_packdelay500_cpu7.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_packdelay500_cpu7.bpf.o name scx_flowcausal_stagewidth_packdelay500_cpu7 > $(TMPDIR)/scx_flowcausal_stagewidth_packdelay500_cpu7.bpf.skel.h

check-bpf-stagewidth-cxx-unbudget:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_cxx_unbudget.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxx_unbudget.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_cxx_unbudget.bpf.o name scx_flowcausal_stagewidth_cxx_unbudget > $(TMPDIR)/scx_flowcausal_stagewidth_cxx_unbudget.bpf.skel.h

check-bpf-stagewidth-cxx-budget20:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_cxx_budget20.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxx_budget20.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_cxx_budget20.bpf.o name scx_flowcausal_stagewidth_cxx_budget20 > $(TMPDIR)/scx_flowcausal_stagewidth_cxx_budget20.bpf.skel.h

check-bpf-stagewidth-parentboost:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentboost.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentboost.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentboost.bpf.o name scx_flowcausal_stagewidth_parentboost > $(TMPDIR)/scx_flowcausal_stagewidth_parentboost.bpf.skel.h

check-bpf-stagewidth-parentboost200:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentboost200.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentboost200.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentboost200.bpf.o name scx_flowcausal_stagewidth_parentboost200 > $(TMPDIR)/scx_flowcausal_stagewidth_parentboost200.bpf.skel.h

check-bpf-stagewidth-parentlatency:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentlatency.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency.bpf.o name scx_flowcausal_stagewidth_parentlatency > $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency.bpf.skel.h

check-bpf-stagewidth-parentlatency-dfl:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentlatency_dfl.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency_dfl.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency_dfl.bpf.o name scx_flowcausal_stagewidth_parentlatency_dfl > $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency_dfl.bpf.skel.h

check-bpf-stagewidth-parentcleanup:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentcleanup.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup.bpf.o name scx_flowcausal_stagewidth_parentcleanup > $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup.bpf.skel.h

check-bpf-stagewidth-parentcleanup-cpu0:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentcleanup_cpu0.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cpu0.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cpu0.bpf.o name scx_flowcausal_stagewidth_parentcleanup_cpu0 > $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cpu0.bpf.skel.h

check-bpf-stagewidth-parentcleanup-nopack-cpu0:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0.bpf.o name scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0 > $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0.bpf.skel.h

check-bpf-stagewidth-parentcleanup-cxxonly-cpu0:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0.bpf.o name scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0 > $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0.bpf.skel.h

check-bpf-stagewidth-parentcleanup-cxxonly-cpu0-200ms:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms.bpf.o name scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms > $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms.bpf.skel.h

check-bpf-stagewidth-spanidle:
	$(CLANG) $(BPF_CFLAGS) -c bpf/scx_flowcausal_stagewidth_spanidle.bpf.c -o $(TMPDIR)/scx_flowcausal_stagewidth_spanidle.bpf.o
	$(BPFTOOL) gen skeleton $(TMPDIR)/scx_flowcausal_stagewidth_spanidle.bpf.o name scx_flowcausal_stagewidth_spanidle > $(TMPDIR)/scx_flowcausal_stagewidth_spanidle.bpf.skel.h

check-user: check-bpf
	$(CC) $(USER_CFLAGS) -c user/scx_flowcausal.c -o $(TMPDIR)/scx_flowcausal.o

check-loader: check-bpf
	$(CC) $(USER_CFLAGS) user/scx_flowcausal.c -o $(TMPDIR)/scx_flowcausal $(LOADER_LDLIBS)

check-loader2: check-bpf2
	$(CC) $(USER_CFLAGS) user/scx_flowcausal2.c -o $(TMPDIR)/scx_flowcausal2 $(LOADER_LDLIBS)

check-loader-auto: check-bpf-auto
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_bpf.c -o $(TMPDIR)/scx_flowcausal_bpf $(LOADER_LDLIBS)

check-loader2-auto: check-bpf2-auto
	$(CC) $(USER_CFLAGS) user/scx_flowcausal2_bpf.c -o $(TMPDIR)/scx_flowcausal2_bpf $(LOADER_LDLIBS)

check-loader3-auto: check-bpf3-auto
	$(CC) $(USER_CFLAGS) user/scx_flowcausal3_bpf.c -o $(TMPDIR)/scx_flowcausal3_bpf $(LOADER_LDLIBS)

check-loader-converge: check-bpf-converge
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_converge.c -o $(TMPDIR)/scx_flowcausal_converge $(LOADER_LDLIBS)

check-loader-stagewidth: check-bpf-stagewidth
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth.c -o $(TMPDIR)/scx_flowcausal_stagewidth $(LOADER_LDLIBS)

check-loader-stagewidth-chainboost: check-bpf-stagewidth-chainboost
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_chainboost.c -o $(TMPDIR)/scx_flowcausal_stagewidth_chainboost $(LOADER_LDLIBS)

check-loader-stagewidth-chainlocal: check-bpf-stagewidth-chainlocal
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_chainlocal.c -o $(TMPDIR)/scx_flowcausal_stagewidth_chainlocal $(LOADER_LDLIBS)

check-loader-stagewidth-cxxfirst500: check-bpf-stagewidth-cxxfirst500
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_cxxfirst500.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500 $(LOADER_LDLIBS)

check-loader-stagewidth-cxxspan2: check-bpf-stagewidth-cxxspan2
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_cxxspan2.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxxspan2 $(LOADER_LDLIBS)

check-loader-stagewidth-cxxfirst500-cpu7: check-bpf-stagewidth-cxxfirst500-cpu7
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_cxxfirst500_cpu7.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxxfirst500_cpu7 $(LOADER_LDLIBS)

check-loader-stagewidth-packdelay500-cpu7: check-bpf-stagewidth-packdelay500-cpu7
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_packdelay500_cpu7.c -o $(TMPDIR)/scx_flowcausal_stagewidth_packdelay500_cpu7 $(LOADER_LDLIBS)

check-loader-stagewidth-cxx-unbudget: check-bpf-stagewidth-cxx-unbudget
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_cxx_unbudget.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxx_unbudget $(LOADER_LDLIBS)

check-loader-stagewidth-cxx-budget20: check-bpf-stagewidth-cxx-budget20
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_cxx_budget20.c -o $(TMPDIR)/scx_flowcausal_stagewidth_cxx_budget20 $(LOADER_LDLIBS)

check-loader-stagewidth-parentboost: check-bpf-stagewidth-parentboost
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentboost.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentboost $(LOADER_LDLIBS)

check-loader-stagewidth-parentboost200: check-bpf-stagewidth-parentboost200
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentboost200.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentboost200 $(LOADER_LDLIBS)

check-loader-stagewidth-parentlatency: check-bpf-stagewidth-parentlatency
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentlatency.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency $(LOADER_LDLIBS)

check-loader-stagewidth-parentlatency-dfl: check-bpf-stagewidth-parentlatency-dfl
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentlatency_dfl.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentlatency_dfl $(LOADER_LDLIBS)

check-loader-stagewidth-parentcleanup: check-bpf-stagewidth-parentcleanup
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentcleanup.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup $(LOADER_LDLIBS)

check-loader-stagewidth-parentcleanup-cpu0: check-bpf-stagewidth-parentcleanup-cpu0
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentcleanup_cpu0.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cpu0 $(LOADER_LDLIBS)

check-loader-stagewidth-parentcleanup-nopack-cpu0: check-bpf-stagewidth-parentcleanup-nopack-cpu0
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_nopack_cpu0 $(LOADER_LDLIBS)

check-loader-stagewidth-parentcleanup-cxxonly-cpu0: check-bpf-stagewidth-parentcleanup-cxxonly-cpu0
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0 $(LOADER_LDLIBS)

check-loader-stagewidth-parentcleanup-cxxonly-cpu0-200ms: check-bpf-stagewidth-parentcleanup-cxxonly-cpu0-200ms
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms.c -o $(TMPDIR)/scx_flowcausal_stagewidth_parentcleanup_cxxonly_cpu0_200ms $(LOADER_LDLIBS)

check-loader-stagewidth-spanidle: check-bpf-stagewidth-spanidle
	$(CC) $(USER_CFLAGS) user/scx_flowcausal_stagewidth_spanidle.c -o $(TMPDIR)/scx_flowcausal_stagewidth_spanidle $(LOADER_LDLIBS)

check-flowcausal2: check-loader2

check-flowcausal-bpf: check-loader-auto

check-flowcausal2-bpf: check-loader2-auto

check-flowcausal3-bpf: check-loader3-auto

check-flowcausal-converge: check-loader-converge

check-flowcausal-stagewidth: check-loader-stagewidth

check-flowcausal-stagewidth-chainboost: check-loader-stagewidth-chainboost

check-flowcausal-stagewidth-chainlocal: check-loader-stagewidth-chainlocal

check-flowcausal-stagewidth-cxxfirst500: check-loader-stagewidth-cxxfirst500

check-flowcausal-stagewidth-cxxspan2: check-loader-stagewidth-cxxspan2

check-flowcausal-stagewidth-cxxfirst500-cpu7: check-loader-stagewidth-cxxfirst500-cpu7

check-flowcausal-stagewidth-packdelay500-cpu7: check-loader-stagewidth-packdelay500-cpu7

check-flowcausal-stagewidth-cxx-unbudget: check-loader-stagewidth-cxx-unbudget

check-flowcausal-stagewidth-cxx-budget20: check-loader-stagewidth-cxx-budget20

check-flowcausal-stagewidth-parentboost: check-loader-stagewidth-parentboost

check-flowcausal-stagewidth-parentboost200: check-loader-stagewidth-parentboost200

check-flowcausal-stagewidth-parentlatency: check-loader-stagewidth-parentlatency

check-flowcausal-stagewidth-parentlatency-dfl: check-loader-stagewidth-parentlatency-dfl

check-flowcausal-stagewidth-parentcleanup: check-loader-stagewidth-parentcleanup

check-flowcausal-stagewidth-parentcleanup-cpu0: check-loader-stagewidth-parentcleanup-cpu0

check-flowcausal-stagewidth-parentcleanup-nopack-cpu0: check-loader-stagewidth-parentcleanup-nopack-cpu0

check-flowcausal-stagewidth-parentcleanup-cxxonly-cpu0: check-loader-stagewidth-parentcleanup-cxxonly-cpu0

check-flowcausal-stagewidth-parentcleanup-cxxonly-cpu0-200ms: check-loader-stagewidth-parentcleanup-cxxonly-cpu0-200ms

check-flowcausal-stagewidth-spanidle: check-loader-stagewidth-spanidle

check-monitor:
	$(CC) $(USER_CFLAGS) user/flowmon.c -o $(TMPDIR)/flowmon $(USER_LDLIBS)

check-daemon:
	$(CC) $(USER_CFLAGS) user/flowd.c -o $(TMPDIR)/flowd $(USER_LDLIBS)

patch-sched-ext: stage
	python3 -c 'from pathlib import Path; path = Path("$(SCHED_EXT)") / "Makefile"; text = path.read_text(); old = "scx_toponest scx_tailguard scx_batch"; new = "scx_toponest scx_tailguard scx_batch scx_flowcausal"; path.write_text(text if "scx_flowcausal" in text else text.replace(old, new))'

dry-run:
	python3 controller/flowctl.py --root-pid $$$$ --dry-run

clean-stage:
	rm -f $(SCHED_EXT)/scx_flowcausal.bpf.c
	rm -f $(SCHED_EXT)/scx_flowcausal.c
	rm -f $(SCHED_EXT)/include/flowcausal.h

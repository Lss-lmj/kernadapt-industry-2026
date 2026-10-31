USER_APP = owner_progress_monitor
BPF_PROG_NAME = owner_progress
USER_SRCS = src/userspace/owner_progress_monitor.c
USER_OBJS = $(patsubst src/userspace/%.c,build/%.o,$(USER_SRCS))
SMOKE_APP = smoke/owner_progress_smoke

CLANG ?= clang
BPFTOOL ?= bpftool

BPF_CFLAGS = -g -O2 -target bpf -I./src/bpf -I../../lock-monitorV3/src/bpf -I/usr/include
USER_CFLAGS = -g -O2 -Wall -I./src/userspace -I./src/bpf -I./build
USER_LDFLAGS = -lbpf -lelf -lz

.PHONY: all clean smoke

all: build_dir $(USER_APP) $(SMOKE_APP)

build_dir:
	@mkdir -p build smoke

$(USER_APP): $(USER_OBJS) build/$(BPF_PROG_NAME).skel.h
	$(CLANG) $(USER_CFLAGS) -o $@ $(USER_OBJS) $(USER_LDFLAGS)

$(USER_OBJS): build/$(BPF_PROG_NAME).skel.h

build/%.o: src/userspace/%.c
	$(CLANG) $(USER_CFLAGS) -c $< -o $@

build/%.skel.h: build/%.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

build/%.bpf.o: src/bpf/$(BPF_PROG_NAME).bpf.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(SMOKE_APP): smoke/owner_progress_smoke.c
	$(CLANG) -O2 -g -Wall -pthread -o $@ $<

smoke: all

clean:
	rm -rf build $(USER_APP) $(SMOKE_APP)

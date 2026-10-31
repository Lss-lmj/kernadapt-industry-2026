// SPDX-License-Identifier: GPL-2.0
/*
 * flowevent_v2: run one tool command while reporting its lifetime to flowd_v2.
 *
 * This program is intentionally self-contained so a statically linked copy can
 * be mounted into an agent sandbox.  It is an unprivileged protocol client;
 * flowd_v2 still authenticates the peer process and its cgroup.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define FLOW_V2_PROTOCOL_VERSION 2

static volatile sig_atomic_t child_pid;
static volatile sig_atomic_t hold_done;

static void forward_signal(int sig)
{
	pid_t pid = (pid_t)child_pid;

	if (pid > 0)
		kill(-pid, sig);
	else
		hold_done = 1;
}

static int parse_u64(const char *name, const char *text, uint64_t *out)
{
	char *end = NULL;
	unsigned long long value;

	errno = 0;
	value = strtoull(text, &end, 0);
	if (errno || !end || *end) {
		fprintf(stderr, "%s 参数无效：%s\n", name, text);
		return -1;
	}
	*out = (uint64_t)value;
	return 0;
}

static int connect_socket(const char *path)
{
	struct sockaddr_un address = {.sun_family = AF_UNIX};
	int fd;

	if (!path || strlen(path) >= sizeof(address.sun_path)) {
		fprintf(stderr, "套接字路径无效\n");
		return -1;
	}
	fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0) {
		perror("无法建立 FlowCausal 事件套接字");
		return -1;
	}
	strcpy(address.sun_path, path);
	if (connect(fd, (struct sockaddr *)&address, sizeof(address))) {
		perror("无法连接 flowd_v2");
		close(fd);
		return -1;
	}
	return fd;
}

static int send_all(int fd, const char *buffer, size_t length)
{
	while (length) {
		ssize_t written = send(fd, buffer, length, MSG_NOSIGNAL);

		if (written < 0 && errno == EINTR)
			continue;
		if (written <= 0)
			return -1;
		buffer += written;
		length -= (size_t)written;
	}
	return 0;
}

static int request(int fd, const char *operation, uint64_t session_id,
		   uint64_t tool_id, uint64_t generation, uint64_t sequence,
		   const char *execution_mode, const char *stage,
		   unsigned int root_pid, bool manual_progress)
{
	char line[512];
	char response[64];
	size_t used = 0;
	int length;

	length = snprintf(line, sizeof(line),
			  "%u %s %llu %llu 0 %llu %llu %s %s %u %u\n",
			  FLOW_V2_PROTOCOL_VERSION, operation,
			  (unsigned long long)session_id,
			  (unsigned long long)tool_id,
			  (unsigned long long)generation,
			  (unsigned long long)sequence, execution_mode, stage,
			  root_pid, manual_progress ? 1U : 0U);
	if (length < 0 || (size_t)length >= sizeof(line) ||
	    send_all(fd, line, (size_t)length)) {
		fprintf(stderr, "发送 FlowCausal 工具事件失败\n");
		return -1;
	}
	while (used + 1 < sizeof(response)) {
		ssize_t count = recv(fd, response + used, 1, 0);

		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0) {
			fprintf(stderr, "flowd_v2 在返回应答前断开\n");
			return -1;
		}
		used += (size_t)count;
		if (response[used - 1] == '\n')
			break;
	}
	response[used] = '\0';
	if (strcmp(response, "OK\n")) {
		fprintf(stderr, "flowd_v2 拒绝工具事件：%s", response);
		return -1;
	}
	return 0;
}

static void usage(const char *program)
{
	fprintf(stderr,
		"用法：%s --socket 路径 --session-id 编号 --tool-id 编号 "
		"--generation 编号 --sequence 序号 [--execution-mode synchronous|asynchronous] "
		"[--stage 阶段] [--manual-progress] [--hold --ready-file 路径] "
		"-- 命令 [参数...]\n",
		program);
}

int main(int argc, char **argv)
{
	const char *socket_path = NULL;
	const char *execution_mode = "synchronous";
	const char *stage = "unknown";
	uint64_t session_id = 0, tool_id = 0, generation = 0, sequence = 0;
	bool manual_progress = false;
	bool hold = false;
	const char *ready_file = NULL;
	struct sigaction action = {.sa_handler = forward_signal};
	int command_index = -1;
	int fd = -1;
	int status = 0;
	pid_t pid;

	for (int index = 1; index < argc; index++) {
		if (!strcmp(argv[index], "--")) {
			command_index = index + 1;
			break;
		} else if (!strcmp(argv[index], "--socket") && index + 1 < argc) {
			socket_path = argv[++index];
		} else if (!strcmp(argv[index], "--session-id") && index + 1 < argc) {
			if (parse_u64("--session-id", argv[++index], &session_id))
				return 2;
		} else if (!strcmp(argv[index], "--tool-id") && index + 1 < argc) {
			if (parse_u64("--tool-id", argv[++index], &tool_id))
				return 2;
		} else if (!strcmp(argv[index], "--generation") && index + 1 < argc) {
			if (parse_u64("--generation", argv[++index], &generation))
				return 2;
		} else if (!strcmp(argv[index], "--sequence") && index + 1 < argc) {
			if (parse_u64("--sequence", argv[++index], &sequence))
				return 2;
		} else if (!strcmp(argv[index], "--execution-mode") && index + 1 < argc) {
			execution_mode = argv[++index];
		} else if (!strcmp(argv[index], "--stage") && index + 1 < argc) {
			stage = argv[++index];
		} else if (!strcmp(argv[index], "--manual-progress")) {
			manual_progress = true;
		} else if (!strcmp(argv[index], "--hold")) {
			hold = true;
		} else if (!strcmp(argv[index], "--ready-file") && index + 1 < argc) {
			ready_file = argv[++index];
		} else {
			usage(argv[0]);
			return 2;
		}
	}
	if (!socket_path || !session_id || !tool_id || !generation || !sequence ||
	    (!hold && (command_index < 0 || command_index >= argc))) {
		usage(argv[0]);
		return 2;
	}

	fd = connect_socket(socket_path);
	if (fd < 0)
		return 125;
	if (request(fd, "tool-begin", session_id, tool_id, generation, sequence,
		    execution_mode, stage, 0, manual_progress)) {
		close(fd);
		return 125;
	}
	if (hold) {
		if (ready_file) {
			int ready_fd = open(ready_file, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
					    0600);

			if (ready_fd < 0) {
				perror("无法写入工具事件就绪文件");
				request(fd, "tool-end", session_id, tool_id, generation,
					sequence + 2, execution_mode, stage, 0,
					manual_progress);
				close(fd);
				return 125;
			}
			close(ready_fd);
		}
		sigemptyset(&action.sa_mask);
		sigaction(SIGUSR1, &action, NULL);
		sigaction(SIGINT, &action, NULL);
		sigaction(SIGTERM, &action, NULL);
		sigaction(SIGHUP, &action, NULL);
		while (!hold_done)
			pause();
		if (request(fd, "tool-end", session_id, tool_id, generation,
			    sequence + 2, execution_mode, stage, 0, manual_progress)) {
			close(fd);
			return 125;
		}
		close(fd);
		return 0;
	}

	pid = fork();
	if (pid < 0) {
		perror("无法启动工具命令");
		request(fd, "tool-end", session_id, tool_id, generation,
			sequence + 2, execution_mode, stage, 0, manual_progress);
		close(fd);
		return 125;
	}
	if (!pid) {
		setpgid(0, 0);
		execvp(argv[command_index], &argv[command_index]);
		perror("无法执行工具命令");
		_exit(errno == ENOENT ? 127 : 126);
	}
	child_pid = pid;
	setpgid(pid, pid);
	if (request(fd, "tool-update", session_id, tool_id, generation,
		    sequence + 1, execution_mode, stage, (unsigned int)pid,
		    manual_progress)) {
		kill(-pid, SIGTERM);
		while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
			;
		close(fd);
		return 125;
	}

	sigemptyset(&action.sa_mask);
	sigaction(SIGINT, &action, NULL);
	sigaction(SIGTERM, &action, NULL);
	sigaction(SIGHUP, &action, NULL);
	while (waitpid(pid, &status, 0) < 0) {
		if (errno != EINTR) {
			perror("等待工具命令失败");
			status = 125 << 8;
			break;
		}
	}
	child_pid = 0;
	if (request(fd, "tool-end", session_id, tool_id, generation,
		    sequence + 2, execution_mode, stage, 0, manual_progress)) {
		close(fd);
		return 125;
	}
	close(fd);
	if (WIFEXITED(status))
		return WEXITSTATUS(status);
	if (WIFSIGNALED(status))
		return 128 + WTERMSIG(status);
	return 125;
}

/* Replacements for the Linux-only parts of osmo-tetra when building tetra-rx for Windows:
 * GSMTAP output (Wireshark feed) and the tun0 network interface are not needed by AndySDR. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <fcntl.h>
#include <stdio.h>
__attribute__((constructor)) static void andysdr_unbuffered(void) { setvbuf(stdout, NULL, _IONBF, 0); }

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <io.h>
/* read/write binary data on stdin, files and pipes (default text mode would corrupt float symbols) */
#include <stdlib.h>
__attribute__((constructor)) static void andysdr_binary_io(void)
{
	_fmode = _O_BINARY;
	_setmode(0, _O_BINARY);
	_setmode(1, _O_BINARY);
}
int inet_aton(const char *cp, struct in_addr *inp)
{
	unsigned long a = inet_addr(cp);
	if (a == INADDR_NONE && strcmp(cp, "255.255.255.255") != 0)
		return 0;
	inp->s_addr = a;
	return 1;
}
#endif

struct msgb;
struct tetra_tdma_time;

struct msgb *tetra_gsmtap_makemsg(struct tetra_tdma_time *tm, int lchan, uint8_t ts, uint8_t ss, int8_t signal_dbm,
				  uint8_t snr, const uint8_t *data, unsigned int len)
{
	(void)tm; (void)lchan; (void)ts; (void)ss; (void)signal_dbm; (void)snr; (void)data; (void)len;
	return NULL;
}

int tetra_gsmtap_sendmsg(struct msgb *msg) { (void)msg; return 0; }
int tetra_gsmtap_init(const char *host, uint16_t port) { (void)host; (void)port; return 0; }
int tun_alloc(char *dev) { (void)dev; return -1; }

/* hooks required by libosmocore's pseudotalloc */
void *pseudotalloc_malloc(size_t size) { return malloc(size); }
void pseudotalloc_free(void *ptr) { free(ptr); }

/* minimal stand-ins for libosmocore logging and panic (msgb.c and a few asserts reference them) */
struct log_info;
struct log_info *osmo_log_info = NULL;
int log_check_level(int subsys, unsigned int level) { (void)subsys; (void)level; return 0; }
void logp2(int subsys, unsigned int level, const char *file, int line, int cont, const char *fmt, ...)
{ (void)subsys; (void)level; (void)file; (void)line; (void)cont; (void)fmt; }
void logp_stub(const char *file, int line, int cont, const char *fmt, ...)
{ (void)file; (void)line; (void)cont; (void)fmt; }
void osmo_panic(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	fprintf(stderr, "tetra-rx panic: ");
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	abort();
}

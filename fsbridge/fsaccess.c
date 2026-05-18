#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "/usr2/fs/include/params.h"
#include "/usr2/fs/include/fs_types.h"
#include "/usr2/fs/include/fscom.h"

extern void skd_run(char name[5], char w, int ip[5]);
extern void cls_snd();

static int ip[5] = { 0, 0, 0, 0, 0};

#define MAX_BUF 512
#define MAX_ERR 256
#define MAX_HOR 30
#define BUFFER_SIZE 2048

#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MIN(a,b) ((a) < (b) ? (a) : (b))


// Detect end of string and add \0
void char2string(char *buf_in, char *buf_out, int max_size)
{
  int i;
  for (i = 0; i <= max_size; i++){
      if (buf_in[i] == ' ')
          break;
      buf_out[i] = buf_in[i];
    }
  buf_out[i] = '\0';
}
// Get LLOG2 data from share memory
const char* get_log_name()
{
    extern struct fscom *shm_addr;
    void setup_ids();
    char *log_name = (char *)malloc(MAX_SKD+1 * sizeof(char));

    setup_ids();  // FS share memory

    char2string(shm_addr->LLOG2, log_name, MAX_SKD);
    return log_name;
}




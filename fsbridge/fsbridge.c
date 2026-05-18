#include <signal.h>
#include <ctype.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <pthread.h>
#include <jansson.h>

#include "/usr2/fs/include/params.h"
#include "/usr2/fs/include/fs_types.h"
#include "/usr2/fs/include/fscom.h"

extern struct fscom *shm_addr;
extern void skd_run(char name[5], char w, int ip[5]);
extern void cls_snd();

static int ip[5] = { 0, 0, 0, 0, 0};

#define MAX_BUF 512
#define MAX_ERR 256
#define MAX_HOR 30
#define BUFFER_SIZE 2048

#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MIN(a,b) ((a) < (b) ? (a) : (b))

char *Logger = NULL;

void log_file(const char *path)
{
  Logger = (char*)malloc(strlen(path) + 1);
  if (Logger != NULL) {
      strcpy(Logger, path);
  }

}
void logit(const char * text)
{
  if (Logger != NULL) {
      FILE *fptr;
      time_t now = time(NULL);
      struct tm *now_tm = localtime(&now);
      char iso_time[30];

      strftime(iso_time, sizeof(iso_time), "%Y-%m-%d %H:%M:%S", now_tm);

      fptr = fopen(Logger, "a");
      fprintf(fptr, "%s - %s\n", iso_time, text);
      fclose(fptr);
  }
}
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
// Execute this SNAP command via "boss".
// Code extracted from inject_snap.c file in FS code.
void inject(const char *command)
{
  static int ip[5] = {0, 0, 0, 0, 0};
  int length = strlen(command);

  logit("inject");
  cls_snd( &(shm_addr->iclopr), command, length, 0, 0);
  skd_run("boss ", 'n', ip);
}
// Get LLOG2 data from share memory
void get_log_name(json_t *response)
{
    char log_name[MAX_SKD+1];

    logit("get_log_name");

    char2string(shm_addr->LLOG2, log_name, MAX_SKD);
    json_object_set_new(response, "log_name", json_string(log_name));
}
// Get LLOG2 data from share memory
void get_schedule_name(json_t *response)
{
    char schedule_name[MAX_SKD+1];

    char2string(shm_addr->LSKD2, schedule_name, MAX_SKD);
    json_object_set_new(response, "schedule_name", json_string(schedule_name));
}
// Get site location
void get_antenna_info(json_t *response)
{
    char ant_name[9];

    logit("get_antenna_info");

    char2string(shm_addr->lnaant, ant_name, 8);
    json_t *ans = json_object();
    json_object_set_new(ans, "name", json_string(ant_name));
    json_object_set_new(ans, "latitude", json_real(shm_addr->alat));
    json_object_set_new(ans, "longitude", json_real(shm_addr->wlong));
    json_object_set_new(ans, "elevation", json_real(shm_addr->height));
    json_object_set_new(ans, "slew1", json_real(shm_addr->slew1));
    json_object_set_new(ans, "slew2", json_real(shm_addr->slew2));
    json_object_set_new(ans, "lolim1", json_real(shm_addr->lolim1));
    json_object_set_new(ans, "lolim2", json_real(shm_addr->lolim2));
    json_object_set_new(ans, "uplim1", json_real(shm_addr->uplim1));
    json_object_set_new(ans, "uplim2", json_real(shm_addr->uplim2));

    // Add mask values
    json_t *mask = json_array();
    for (int i = 0; i < MAX_HOR; i++){
        if (shm_addr->horaz[i] > -1.0)
            json_array_append(mask, json_real(shm_addr->horaz[i]));
        if (shm_addr->horel[i] > -1.0)
            json_array_append(mask, json_real(shm_addr->horel[i]));
    }
    json_object_set_new(ans, "mask", mask);
    json_object_set_new(response, "antenna", ans);
}
// Test if FS is running. Add error message to response
int check_fs()
{
    int nsem_test();

    return nsem_test("fs   ") == 1;
}
// Dump json data into response buffer
void send_response(int sock, json_t *response, char *buffer)
{
    memset(buffer, 0, BUFFER_SIZE); // Clear the buffer
    char *str = json_dumps(response, JSON_COMPACT);
    json_decref(response);

    if (str) {
        memcpy(buffer, str, strlen(str));
        free(str); // Free the allocated string
    }
    else{
        str = "{error: problem dumping json}";
        memcpy(buffer, str, strlen(str));
    }
    write(sock, buffer, strlen(buffer));
    memset(buffer, 0, BUFFER_SIZE); // Clear the buffer
    logit("response sent");
}
// Get json data from client
json_t * get_client_request(json_t *response, char *buffer)
{
    json_t *request;
    json_error_t error;

    if (!(request = json_loads(buffer, 0, &error))){
        char err_msg[MAX_ERR];
        sprintf(err_msg, "error decoding message: %s", error.text);
        json_object_set_new(response, "error", json_string(err_msg));
    }
    return request;
}
// Process client action
void process_action(json_t *data, json_t *response, int fs_running)
{
    const char *action;
    json_t *value = json_object_get(data, "action");
    if (!value) {
        return;
    }

    action = json_string_value(value);
    //json_decref(value);

    if (strcmp(action, "inject") == 0) {
        if (fs_running)
            inject(json_string_value(json_object_get(data, "command")));
    } else if (strcmp(action, "log_name") == 0) {
        get_log_name(response);
    } else if (strcmp(action, "schedule_name") == 0) {
        get_schedule_name(response);
    } else if (strcmp(action, "antenna") == 0) {
        get_antenna_info(response);
    } else {
        json_object_set_new(response, action, json_string("error: invalid request"));
    }
}
// Thread function to handle individual client connections
void *client_processing(void *socket_desc) {
    int sock = *(int*)socket_desc;
    char buffer[BUFFER_SIZE];
    const char * action;
    int read_size;
    json_t *request;
    json_t *response;
    int fs_running;

    // Receive a message from client
    while ((read_size = recv(sock, buffer, BUFFER_SIZE, 0)) > 0) {
        // process message in json format
        logit("message received");
        response = json_object();  // Initialize response
        fs_running = check_fs();
        json_object_set_new(response, "fs", json_boolean(fs_running)); // Add fs_status in all responses

        if (request = get_client_request(response, buffer)){
            if (!json_is_array(request)) {
                process_action(request, response, fs_running);
            } else { // many commands were sent in same request
                json_t *data;
                size_t index;
                json_array_foreach(request, index, data){
                    process_action(data, response, fs_running);
                    json_decref(data);
                }
            }
            json_decref(request);
       }
        // Send response to client
        send_response(sock, response, buffer);
    }
    close(sock);
    free(socket_desc); // Free the dynamically allocated socket descriptor
    logit("client socket closed");
    return NULL;
}
// The signal handler function
void terminate(int _) {
    (void)_; // Suppress unused parameter warning
    logit("close socket");
    // close(client_sock);
    logit("program terminated");
    exit(0); // Set the flag to 0 to stop the loop
}
// Main app. Socket server waiting for input
int main(int argc, char *argv[]) {
    int socket_desc,  client_sock, size_socket, *new_sock;
    struct sockaddr_in server, client;
    void setup_ids();

    // extract ip address and port
    if (argc < 3) {
        printf("usage: shmserver host port");
        logit("bad usage");
        exit(1);
    }
    // Set log file if argc > 3
    if (argc > 4 && argv[3] == "-l") {
       log_file(argv[4]);
    }

    // FS share memory
    setup_ids();
    // Set signal handler
    signal(SIGINT, terminate);
    // Create socket
    if ((socket_desc = socket(AF_INET, SOCK_STREAM, 0)) == -1) {
        logit("Could not create socket\n");
        exit(-1);
    }
    // Define address and port.
    server.sin_family = AF_INET;
    inet_pton(AF_INET, argv[1], &server.sin_addr);
    server.sin_port = htons(atoi(argv[2]));

    // Bind
    if (bind(socket_desc, (struct sockaddr *)&server, sizeof(server)) < 0) {
        logit("Could not bind socket");
        exit(-1);
    }
    // Listen and accept incoming connections
    listen(socket_desc, 3);
    size_socket = sizeof(struct sockaddr_in);

    while ((client_sock = accept(socket_desc, (struct sockaddr *)&client, (socklen_t*)&size_socket))) {
        // Connection accepted
        pthread_t client_thread;
        pthread_attr_t attr;
        //pthread_t *client_thread = (pthread_t *)malloc(sizeof(pthread_t));
        new_sock = malloc(1);
        *new_sock = client_sock;

        // Set attribute detach to automatically clear memory
        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, 1);
        if (pthread_create(&client_thread, &attr, client_processing, (void*) new_sock) < 0) {
            logit("could not create thread for client");
            exit(-1);
        }
        pthread_attr_destroy(&attr);
    }

    if (client_sock < 0) {
        logit("accept failed");
        return 1;
    }

    return 0;
}


import asyncio
import csv
import logging
import os
import threading
import time
from queue import Queue
import OpenOPC
import pythoncom
from asyncua import Server, ua

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
)
logger = logging.getLogger("OPC_MultiServer_Gateway")

# --- CONFIGURATION ---
CSV_FILE_PATH = "tags.csv"
OPC_UA_ENDPOINT = "opc.tcp://0.0.0.0:4840/freeopcua/server/"
OPC_UA_NAMESPACE = "http://mycompany.com"

CHUNK_SIZE = 500       
POLL_INTERVAL = 0.5    
RECONNECT_DELAY = 5.0  

# Global thread-safe outbound queue map: { "server_name": Queue() }
write_queues = {}
async_loop = None

class MultiServerWriteHandler:
    """Routes modern OPC UA client writes back to the correct physical server's queue."""
    def __init__(self, tag_routing_map):
        # Maps nodeid -> (server_name, da_tag)
        self.tag_routing_map = tag_routing_map

    async def write_data_value(self, nodeid, value):
        route = self.tag_routing_map.get(nodeid)
        if route:
            server_name, da_tag = route
            new_val = value.Value.Value
            # Push specifically into that target server's dedicated queue
            write_queues[server_name].put((da_tag, new_val))
            logger.info(f"Queued write for Server [{server_name}] -> Tag: {da_tag} | Val: {new_val}")


def load_multi_server_config(file_path):
    """Parses CSV and groups tags by their unique target server name."""
    config = {} # Structure: { "server_name": [tag1, tag2...] }
    if not os.path.exists(file_path):
        logger.error(f"Configuration file missing: '{file_path}'.")
        return config

    with open(file_path, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            server = row.get('server_name', '').strip()
            tag = row.get('da_tag', '').strip()
            if server and tag:
                if server not in config:
                    config[server] = []
                config[server].append(tag)
    return config


def opc_da_worker(server_name, tags, ua_variables, ua_status_node):
    """Dedicated Windows COM/DCOM pipeline loop isolated to a single server instance."""
    global async_loop
    pythoncom.CoInitialize() # Bind thread to Windows STA Model
    
    my_queue = write_queues[server_name]
    chunks = [tags[i:i + CHUNK_SIZE] for i in range(0, len(tags), CHUNK_SIZE)]
    da_client = None
    is_connected = False

    while True:
        # STATE 1: Disconnected / Reconnecting
        if not is_connected:
            # Set internal OPC UA monitoring variable to False thread-safely
            if async_loop:
                asyncio.run_coroutine_threadsafe(ua_status_node.write_value(False), async_loop)
                
            logger.info(f"Connecting to legacy server: [{server_name}]...")
            try:
                da_client = OpenOPC.client()
                da_client.connect(server_name)
                da_client.create_group(f'Group_{server_name.replace(".", "_")}')
                is_connected = True
                
                if async_loop:
                    asyncio.run_coroutine_threadsafe(ua_status_node.write_value(True), async_loop)
                logger.info(f"DCOM Connection established successfully to [{server_name}].")
            except Exception as conn_err:
                logger.error(f"Connection to [{server_name}] failed: {conn_err}. Retrying...")
                time.sleep(RECONNECT_DELAY)
                continue

        # STATE 2: Connected Operational Sync Loop
        try:
            # 1. Process Outbound Write Tasks
            while not my_queue.empty():
                da_tag, value = my_queue.get_nowait()
                try:
                    da_client.write((da_tag, value))
                    logger.info(f"[{server_name}]: Wrote {value} to {da_tag}")
                except Exception as write_err:
                    logger.error(f"[{server_name}]: Write failed for {da_tag}: {write_err}")
                my_queue.task_done()

            # 2. Process Inbound Telemetry Reads
            for chunk in chunks:
                da_data = da_client.read(chunk)
                for tag_name, value, quality, timestamp in da_data:
                    if quality == "Good" and async_loop:
                        ua_node = ua_variables[tag_name]
                        asyncio.run_coroutine_threadsafe(ua_node.write_value(value), async_loop)
                    elif quality != "Good":
                        logger.warning(f"[{server_name}] Tag {tag_name} quality bad: {quality}")
            
            time.sleep(POLL_INTERVAL)

        except (OpenOPC.OpcError, Exception) as runtime_err:
            logger.error(f"Connection broken on server [{server_name}]: {runtime_err}. Resetting client.")
            is_connected = False
            if da_client:
                try:
                    da_client.close()
                except Exception:
                    pass
            time.sleep(RECONNECT_DELAY)


async def main():
    global async_loop
    async_loop = asyncio.get_running_loop()

    # 1. Parse Config Schema
    server_config = load_multi_server_config(CSV_FILE_PATH)
    if not server_config:
        logger.error("No valid server configurations parsed from CSV. Exiting.")
        return

    # 2. Start UA Engine Setup
    ua_server = Server()
    await ua_server.init()
    ua_server.set_endpoint(OPC_UA_ENDPOINT)
    idx = await ua_server.register_namespace(OPC_UA_NAMESPACE)
    root_folder = await ua_server.nodes.objects.add_object(idx, "MultiServer_OPC_Bridge")
    
    tag_routing_map = {}

    # 3. Dynamic Address Space Architecture Grouped By Server
    for server_name, tags in server_config.items():
        logger.info(f"Configuring memory footprints for server: {server_name}")
        
        # Initialize an independent thread queue for this specific server
        write_queues[server_name] = Queue()
        
        # Create an isolating folder wrapper inside OPC UA namespace for clarity
        clean_server_name = server_name.replace(".", "_").replace(" ", "_")
        server_folder = await root_folder.add_object(idx, f"Server_{clean_server_name}")
        
        # Add a diagnostic status node inside this specific folder
        status_node = await server_folder.add_variable(idx, "IsConnected", False)
        
        server_ua_variables = {}
        for tag in tags:
            clean_tag_name = tag.replace(".", "_").replace(" ", "_")
            ua_node = await server_folder.add_variable(idx, clean_tag_name, 0.0)
            await ua_node.set_writable()
            
            server_ua_variables[tag] = ua_node
            tag_routing_map[ua_node.nodeid] = (server_name, tag)

        # 4. Spawn a dedicated, independent Worker Thread for this server context
        worker_thread = threading.Thread(
            target=opc_da_worker,
            args=(server_name, tags, server_ua_variables, status_node),
            name=f"Worker_{clean_server_name}",
            daemon=True
        )
        worker_thread.start()

    # 5. Bind Interceptor Callback
    handler = MultiServerWriteHandler(tag_routing_map)
    ua_server.aspace.set_data_value_callback(handler.write_data_value)

    # 6. Keep Running Async Server Loop
    logger.info("Multi-server OPC UA Gateway engine is online.")
    async with ua_server:
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("System shutting down.")

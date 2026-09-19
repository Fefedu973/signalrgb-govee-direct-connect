"""Send one local API message; does not contain device configuration or keys."""
import argparse,json,socket
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('message',help='JSON object matching the bridge API')
parser.add_argument('--port',type=int,default=47684)
args=parser.parse_args()
message=json.loads(args.message)
if not isinstance(message,dict):raise SystemExit('Expected a JSON object')
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
    sock.bind(('127.0.0.1',0));sock.settimeout(13)
    sock.sendto(json.dumps(message).encode(),('127.0.0.1',args.port))
    data,_=sock.recvfrom(16384)
    print(json.dumps(json.loads(data),indent=2))

// Preload for this review's offline tests. Never imported by production code.
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import {syncBuiltinESMExports} from 'node:module';
const blocked=()=>{throw Error('network disabled in V2.1 offline review tests');};
net.connect=blocked;net.createConnection=blocked;net.Socket.prototype.connect=blocked;
net.Server.prototype.listen=blocked;tls.connect=blocked;
http.request=blocked;http.get=blocked;https.request=blocked;https.get=blocked;
globalThis.fetch=blocked;
syncBuiltinESMExports();

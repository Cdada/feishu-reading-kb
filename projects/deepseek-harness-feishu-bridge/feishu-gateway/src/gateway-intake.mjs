import {createAttachmentReceiver} from './attachment-store.mjs';
import {IntakeFlow} from './intake-flow.mjs';

// Shared by gateway.mjs and the offline integration harness. The running V1
// gateway keeps the default; V2.1 activation requires explicit isolated adapters.
export function createGatewayIntakeFlow({mode='v1',channel,inboxDir,custodyBridge=null,...options}) {
  const receiver=createAttachmentReceiver({mode,channel,inboxDir,custodyBridge});
  return new IntakeFlow({...options,ingestionMode:mode,
    custodyIntake:mode==='v2.1-dev'?receiver:null,
    download:message=>receiver.receive(message)});
}

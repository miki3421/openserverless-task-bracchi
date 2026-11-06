// Public export executable only. Data, arguments/logs and working files stay
// in protected staging; never relax a noexec mount or use a loader bypass.
import {accessSync,chmodSync,copyFileSync,constants,lstatSync,mkdtempSync,rmSync} from "node:fs";
import {dirname,isAbsolute,join,resolve} from "node:path";
import {artifactInfo} from "./update/portable";
import {runPrivate,type PrivateRunner} from "./update/storage";

export function verifyWeedToolParent(parent:string) {
  if(!isAbsolute(parent)) throw Error("absolute_export_tool_parent_required");
  for(let p=resolve(parent);;p=dirname(p)) {
    if(lstatSync(p).isSymbolicLink()) throw Error("linked_export_tool_parent");
    if(p===dirname(p))break;
  }
  const st=lstatSync(parent);
  if(!st.isDirectory() || st.uid!==process.getuid?.() || st.mode&0o077) throw Error("private_export_tool_parent_required");
  accessSync(parent,constants.W_OK|constants.X_OK);
}

export function ephemeralWeedRunner(parent:string,execute:PrivateRunner=runPrivate) {
  verifyWeedToolParent(parent);
  let directory:string|undefined,source:string|undefined,checksum:string|undefined;
  const run:PrivateRunner=async(program,args,output,label,input)=>{
    if(!directory) {
      const info=lstatSync(program);
      if(!info.isFile() || info.isSymbolicLink() || info.nlink!==1 || info.uid!==process.getuid?.() ||
          info.mode&0o077 || info.size<20 || info.size>256*1024**2 || resolve(program)!==join(resolve(output),"weed"))
        throw Error("invalid_public_export_tool");
      source=program;checksum=(await artifactInfo(output,program)).sha256;
      directory=mkdtempSync(join(parent,"ops-data-weed-"));chmodSync(directory,0o700);
      const cached=join(directory,"weed");copyFileSync(program,cached,constants.COPYFILE_EXCL);chmodSync(cached,0o700);
      if((await artifactInfo(directory,cached)).sha256!==checksum) throw Error("export_tool_copy_mismatch");
    }
    if(program!==source || (await artifactInfo(output,program)).sha256!==checksum ||
        (await artifactInfo(directory,join(directory,"weed"))).sha256!==checksum) throw Error("export_tool_changed");
    // The caller's native collector has already verified the source ELF and
    // host/node architecture. This only changes the executable's location.
    await execute(join(directory,"weed"),args,output,label,input);
  };
  return {run,dispose:()=>{if(directory){rmSync(directory,{recursive:true});directory=undefined;}}};
}

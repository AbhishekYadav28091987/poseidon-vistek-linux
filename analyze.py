#!/usr/bin/env python3
"""Static analysis helper for TempComm.dll (and friends)."""
import sys, pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

PATH = sys.argv[1] if len(sys.argv) > 1 else "extracted/payload/app/TempComm.dll"
pe = pefile.PE(PATH, fast_load=False)
base = pe.OPTIONAL_HEADER.ImageBase

# ---- Build IAT map: VA-of-pointer -> imported name ----
iat = {}
if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
    for mod in pe.DIRECTORY_ENTRY_IMPORT:
        dll = mod.dll.decode(errors="replace")
        for imp in mod.imports:
            nm = imp.name.decode(errors="replace") if imp.name else f"ord{imp.ordinal}"
            iat[imp.address] = f"{dll}!{nm}"  # imp.address is the VA of the IAT slot

# ---- Disassemble .text ----
text = next(s for s in pe.sections if b".text" in s.Name)
code = text.get_data()
text_va = base + text.VirtualAddress
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

insns = list(md.disasm(code, text_va))
by_addr = {i.address: idx for idx, i in enumerate(insns)}

def resolve_rip(ins):
    """If instruction has a rip-relative mem operand, return its absolute target VA."""
    for op in ins.operands:
        if op.type == 3 and op.mem.base == md.reg_name(ins.reg_name and 0):  # placeholder
            pass
    # simpler: parse from op_str for 'rip + 0x..'
    return None

def rip_target(ins):
    # capstone: for x86, mem with base == rip
    from capstone.x86 import X86_OP_MEM, X86_REG_RIP
    for op in ins.operands:
        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
            return ins.address + ins.size + op.mem.disp
    return None

CMD = sys.argv[2] if len(sys.argv) > 2 else "imports"

if CMD == "iat":
    for va, nm in sorted(iat.items()):
        print(f"{va:#x}  {nm}")

elif CMD == "callsites":
    # show every indirect call/jmp whose target IAT slot is a named import
    want = sys.argv[3] if len(sys.argv) > 3 else ""
    for ins in insns:
        if ins.mnemonic in ("call", "jmp"):
            t = rip_target(ins)
            if t in iat and (want.lower() in iat[t].lower()):
                print(f"{ins.address:#x}: {ins.mnemonic} {iat[t]}")

elif CMD == "func":
    # disassemble from a start VA until ret/4 rets, resolving imports + rip targets
    start = int(sys.argv[3], 0)
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 80
    idx = by_addr.get(start)
    if idx is None:
        # find nearest
        idx = min(range(len(insns)), key=lambda k: abs(insns[k].address - start))
    for ins in insns[idx:idx+n]:
        t = rip_target(ins)
        ann = ""
        if t is not None:
            if t in iat:
                ann = f"   ; {iat[t]}"
            else:
                ann = f"   ; ->{t:#x}"
        print(f"{ins.address:#x}: {ins.mnemonic} {ins.op_str}{ann}")
        if ins.mnemonic == "ret":
            break

elif CMD == "xref":
    # find instructions referencing a given VA (rip-relative)
    target = int(sys.argv[3], 0)
    for ins in insns:
        if rip_target(ins) == target:
            print(f"{ins.address:#x}: {ins.mnemonic} {ins.op_str}")

elif CMD == "imm":
    # find immediates
    val = int(sys.argv[3], 0)
    for ins in insns:
        if f"0x{val:x}" in ins.op_str:
            print(f"{ins.address:#x}: {ins.mnemonic} {ins.op_str}")

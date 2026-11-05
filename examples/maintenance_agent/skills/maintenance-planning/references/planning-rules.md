# Synthetic planning rules — revision 1

Each pump requires one technician throughout its inspection. Each technician handles one pump at a time. Every selected pump must be included; do not split an inspection between windows.
Default inspection duration is 30 minutes per pump. A later user-specified duration overrides it.
There is one 30-minute setup period for the whole crew, performed concurrently before inspections. It does not multiply by the number of technicians.
Total inspection effort = selected pumps × inspection minutes per pump.
Elapsed time = 30 setup minutes + ceil(selected pumps / technicians) × inspection minutes per pump.
The plan fits only when elapsed time is no greater than the user's window. User exclusions remove pumps from scope, not from the asset inventory. Preserve those exclusions on later turns unless the user changes them.
This rule set and all sample asset data are synthetic, not engineering advice or real maintenance instructions.

## Usage 

```bash
python extract.py mod/top.sv -I ./mod -o my_slice.sv --name my_slice
python inline.py mod/top.v -I mod -I . --module my_slice -o rtl/top_inlined.sv
```

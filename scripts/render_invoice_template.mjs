import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
if(!process.env.INVOICE_NODE_MODULES) throw Error('Missing INVOICE_NODE_MODULES from bundled runtime');
const require=createRequire(path.resolve(process.env.INVOICE_NODE_MODULES,'../invoice-runtime.cjs'));
const {FileBlob,SpreadsheetFile}=await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);

const [inputPath,payloadPath,outputPath]=process.argv.slice(2);
if (!outputPath || process.argv.length !== 5) throw Error('Usage: render_invoice_template.mjs light.xlsx payload.json authored.xlsx');
const {schema,rows}=JSON.parse(await fs.readFile(payloadPath,'utf8'));
const workbook=await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
for (const [name,meta] of Object.entries(schema)) {
  const sheet=workbook.worksheets.getItem(name);
  for (const [label,col] of Object.entries(meta.columns)) {
    const values=rows[name].map(row=>[row[label]??'']);
    if (!values.length) continue;
    const range=sheet.getRange(`${col}${meta.header_row+1}:${col}${meta.header_row+values.length}`);
    range.values=values;
    if (JSON.stringify(range.values)!==JSON.stringify(values)) throw Error(`写入回读不一致: ${name}/${label}`);
  }
}
workbook.recalculate();
const output=await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({authored:true,rows:Object.fromEntries(Object.entries(rows).map(([k,v])=>[k,v.length]))}));

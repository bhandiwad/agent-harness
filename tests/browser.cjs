/* Run against an explicitly configured disposable fixture preview. Never a customer session. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
  const output=process.env.GENERIC_UI_OUTPUT;
  assert(output && process.env.GENERIC_UI_URL, 'Explicit fixture URL and output directory are required');
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH,headless:true});
  const context=await browser.newContext({viewport:{width:1280,height:1000}});
  const page=await context.newPage();const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const checks=[];
  async function connect(subject){await page.locator('#token').fill(fs.readFileSync(output+'/'+subject+'.token','utf8'));await page.locator('#connect').click();await page.locator('#connection').filter({hasText:'Connected'}).waitFor();}
  async function status(value){await page.waitForFunction(value=>document.querySelector('#status').textContent.startsWith(value),value,{timeout:30000});}
  async function send(text){await page.locator('#message').fill(text);await page.locator('#send').click();await page.waitForFunction(text=>document.querySelector('#messages').innerText.includes('You: '+text),text);}
  try{
    await page.goto(process.env.GENERIC_UI_URL);await connect('alice');
    await page.locator('#new').click();await status('idle');
    const id=await page.locator('#session-id').inputValue();
    await send('Look up asset A17');await page.locator('#messages').filter({hasText:'Generating — provisional: A17 ha'}).waitFor();checks.push('provider delta visible before completion');await status('completed');
    assert.match(await page.locator('#messages').innerText(),/8 pumps.*fixture:\/\/assets\/A17/);checks.push('source-backed answer through fixture context adapter');
    await page.reload();assert.equal(await page.locator('#token').inputValue(),'');await connect('alice');
    await page.locator('#sessions').selectOption(id);await status('completed');
    await send('How many did you say?');await status('completed');
    assert.match(await page.locator('#messages').innerText(),/previous context said 8 pumps/);checks.push('reload and persisted follow-up');
    await page.getByText('Workspace files',{exact:true}).click();
    await page.locator('#file').setInputFiles({name:'notes.txt',mimeType:'text/plain',buffer:Buffer.from('<script>private fixture notes</script>')});
    await page.locator('#upload').click();await page.locator('#files').filter({hasText:'notes.txt'}).waitFor();
    const downloadPromise=page.waitForEvent('download');await page.locator('#files button').click();const download=await downloadPromise;
    assert.equal(download.suggestedFilename(),'notes.txt');checks.push('text artifact upload and attachment download');
    await send('Open case for the pump issue');await status('waiting approval');
    assert.equal(await page.locator('#approve').isDisabled(),true);checks.push('self-approval disabled in UI; server rejection covered by engine tests');
    await connect('carol');await page.waitForFunction(()=>!document.querySelector('#approve').disabled);await page.locator('#reason').fill('Synthetic independent review');await page.locator('#approve').click();await status('completed');
    assert.match(await page.locator('#messages').innerText(),/FIXTURE-17 confirmed/);checks.push('second reviewer approval resumes action');
    await connect('alice');await send('Stream a plain answer');await page.locator('#messages').filter({hasText:'Generating — provisional:'}).waitFor();await page.locator('#cancel').click();await status('cancelled');assert.doesNotMatch(await page.locator('#messages').innerText(),/Agent: Synthetic fixture answer/);checks.push('cancelled stream never becomes a completed answer');
    assert.equal(await page.evaluate(()=>localStorage.length),0);checks.push('token not persisted to browser localStorage');
    await page.locator('#token').fill('');
    await page.screenshot({path:output+'/generic-chat.png',fullPage:true});
    assert.deepEqual(errors,[]);checks.push('no browser page errors');
    fs.writeFileSync(output+'/browser-validation.json',JSON.stringify({fixture_only:true,checks,passed:checks.length},null,2));
    console.log(JSON.stringify({passed:checks.length,checks}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});

WebSocket echo test for Render

Purpose
----
Deploy this minimal WebSocket echo server to Render to verify whether Render allows long-running WebSocket connections and background processes required for remote browser streaming.

How to deploy to Render
----
1. Create a new Web Service on Render and connect it to this repository or copy these files into a fresh repo.
2. Use the Dockerfile as the build and start settings (Render will build the Docker image).
3. Set the service to be a "Web Service" (not background worker) and deploy.

Test the WebSocket
----
Once the service is deployed, get its URL (e.g., wss://your-service.onrender.com) and test with wscat (or a simple browser console):

Using wscat:

    wscat -c wss://your-service.onrender.com
    > hello
    < echo:hello

Using browser console:

    const ws = new WebSocket('wss://your-service.onrender.com');
    ws.onopen = () => ws.send('hello');
    ws.onmessage = (m) => console.log('msg', m.data);

If you get an echo, Render allows WebSocket and long-running processes for this service type.

Notes
----
- This is intentionally minimal. If Render blocks WebSocket or background processes, you'll get connection errors.
- If the test passes, we can proceed to implement a Playwright websocket proxy or noVNC flow.

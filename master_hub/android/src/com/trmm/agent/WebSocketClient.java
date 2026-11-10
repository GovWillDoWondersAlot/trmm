package com.trmm.agent;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Base64;
import javax.net.ssl.SSLSocketFactory;

public class WebSocketClient {
    public interface Listener {
        void onOpen();
        void onMessage(String text);
        void onClose(int code, String reason);
        void onError(Exception ex);
    }

    static class ReadRunnable implements Runnable {
        private final WebSocketClient client;

        public ReadRunnable(WebSocketClient client) {
            this.client = client;
        }

        @Override
        public void run() {
            client.readLoop();
        }
    }

    private final URI uri;
    private final Listener listener;
    private final java.util.Random random = new java.util.Random();
    private Socket socket;
    private OutputStream out;
    private InputStream in;
    private volatile boolean connected = false;
    private volatile long lastActivityTime = 0L;
    private Thread readThread;

    public WebSocketClient(URI uri, Listener listener) {
        this.uri = uri;
        this.listener = listener;
    }

    public synchronized void connect() throws Exception {
        if (connected) return;
        String host = uri.getHost();
        int port = uri.getPort();
        boolean isSsl = "wss".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme());
        if (port <= 0) {
            port = isSsl ? 443 : 80;
        }

        if (isSsl) {
            socket = SSLSocketFactory.getDefault().createSocket(host, port);
        } else {
            socket = new Socket(host, port);
        }
        socket.setTcpNoDelay(true);
        try {
            socket.setKeepAlive(true);
            socket.setSoTimeout(8000); // 8-second heartbeat read timeout
        } catch (Throwable ignored) {}
        out = socket.getOutputStream();
        in = socket.getInputStream();

        // Generate Sec-WebSocket-Key
        byte[] keyBytes = new byte[16];
        new SecureRandom().nextBytes(keyBytes);
        String secKey = Base64.getEncoder().encodeToString(keyBytes);

        String path = uri.getRawPath();
        if (path == null || path.isEmpty()) path = "/";
        if (uri.getRawQuery() != null) path += "?" + uri.getRawQuery();

        // Send HTTP Handshake
        String handshake = "GET " + path + " HTTP/1.1\r\n" +
                "Host: " + host + (port != 80 && port != 443 ? ":" + port : "") + "\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Key: " + secKey + "\r\n" +
                "Sec-WebSocket-Version: 13\r\n" +
                "User-Agent: TRMM-Android-Agent/3.0\r\n\r\n";
        out.write(handshake.getBytes(StandardCharsets.UTF_8));
        out.flush();

        // Read HTTP response headers
        StringBuilder headerBuf = new StringBuilder();
        int b;
        while ((b = in.read()) != -1) {
            headerBuf.append((char) b);
            if (headerBuf.toString().endsWith("\r\n\r\n")) break;
        }

        String headers = headerBuf.toString();
        if (!headers.contains("101")) {
            close();
            throw new IllegalStateException("Handshake failed: " + headers.split("\r\n")[0]);
        }

        connected = true;
        lastActivityTime = System.currentTimeMillis();
        if (listener != null) listener.onOpen();

        readThread = new Thread(new ReadRunnable(this), "TRMM-WS-Reader");
        readThread.setDaemon(true);
        readThread.start();
    }

    void readLoop() {
        try {
            while (connected && socket != null && !socket.isClosed()) {
                int b0;
                try {
                    b0 = in.read();
                } catch (SocketTimeoutException e) {
                    long elapsed = System.currentTimeMillis() - lastActivityTime;
                    if (elapsed > 20000L) {
                        // Dead socket: no data or ping response received within 20s
                        break;
                    }
                    sendPing();
                    continue;
                }
                if (b0 == -1) break;
                lastActivityTime = System.currentTimeMillis();
                int opcode = b0 & 0x0F;

                int b1 = in.read();
                if (b1 == -1) break;
                lastActivityTime = System.currentTimeMillis();
                boolean isMasked = (b1 & 0x80) != 0;
                long len = b1 & 0x7F;

                if (len == 126) {
                    len = ((in.read() & 0xFF) << 8) | (in.read() & 0xFF);
                } else if (len == 127) {
                    len = 0;
                    for (int i = 0; i < 8; i++) {
                        len = (len << 8) | (in.read() & 0xFF);
                    }
                }

                byte[] mask = null;
                if (isMasked) {
                    mask = new byte[4];
                    readFully(in, mask);
                }

                byte[] data = new byte[(int) len];
                readFully(in, data);
                lastActivityTime = System.currentTimeMillis();
                if (isMasked && mask != null) {
                    for (int i = 0; i < data.length; i++) {
                        data[i] ^= mask[i % 4];
                    }
                }

                if (opcode == 0x1) { // Text frame
                    String text = new String(data, StandardCharsets.UTF_8);
                    if (listener != null) listener.onMessage(text);
                } else if (opcode == 0x8) { // Close
                    int code = data.length >= 2 ? ((data[0] & 0xFF) << 8) | (data[1] & 0xFF) : 1000;
                    if (listener != null) listener.onClose(code, "Server closed");
                    break;
                } else if (opcode == 0x9) { // Ping from server -> reply with Pong
                    sendFrame(0xA, data);
                } else if (opcode == 0xA) { // Pong from server
                    lastActivityTime = System.currentTimeMillis();
                }
            }
        } catch (Exception e) {
            if (connected && listener != null) {
                listener.onError(e);
            }
        } finally {
            close();
            if (listener != null) {
                listener.onClose(1006, "Connection closed or timed out");
            }
        }
    }

    public synchronized void send(String text) {
        if (!connected || out == null) return;
        try {
            byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
            sendFrame(0x1, bytes);
        } catch (Exception e) {
            if (listener != null) listener.onError(e);
            close();
        }
    }

    public synchronized void sendPing() {
        if (!connected || out == null) return;
        try {
            sendFrame(0x9, new byte[0]);
        } catch (Throwable ignored) {}
    }

    public synchronized void sendBinary(byte[] bytes) {
        if (!connected || out == null) return;
        try {
            sendFrame(0x2, bytes);
        } catch (Exception e) {
            if (listener != null) listener.onError(e);
            close();
        }
    }

    private synchronized void sendFrame(int opcode, byte[] payload) throws Exception {
        ByteArrayOutputStream frame = new ByteArrayOutputStream();
        frame.write(0x80 | (opcode & 0x0F)); // FIN + opcode

        byte[] mask = new byte[4];
        random.nextBytes(mask);

        int len = payload.length;
        if (len <= 125) {
            frame.write(0x80 | len); // MASK = 1
        } else if (len <= 65535) {
            frame.write(0x80 | 126);
            frame.write((len >> 8) & 0xFF);
            frame.write(len & 0xFF);
        } else {
            frame.write(0x80 | 127);
            for (int i = 7; i >= 0; i--) {
                frame.write((int) ((len >> (i * 8)) & 0xFF));
            }
        }

        frame.write(mask);
        byte[] masked = new byte[len];
        for (int i = 0; i < len; i++) {
            masked[i] = (byte) (payload[i] ^ mask[i % 4]);
        }
        frame.write(masked);

        out.write(frame.toByteArray());
        out.flush();
    }

    private void readFully(InputStream in, byte[] buf) throws Exception {
        int read = 0;
        while (read < buf.length) {
            int r = in.read(buf, read, buf.length - read);
            if (r == -1) throw new IllegalStateException("Unexpected EOF");
            read += r;
        }
    }

    public synchronized void close() {
        connected = false;
        try {
            if (socket != null && !socket.isClosed()) {
                socket.close();
            }
        } catch (Exception ignored) {}
    }

    public boolean isConnected() {
        return connected && (System.currentTimeMillis() - lastActivityTime < 25000L);
    }

    public long getLastActivityTime() {
        return lastActivityTime;
    }
}

"""
Minimal HTTP wrapper around our agent.
WHY not Flask/FastAPI yet? Because you need to understand what they
abstract away before you use them. This is raw Python HTTP.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from src.agent import ask_agent


class AgentHandler(BaseHTTPRequestHandler):
    
    def do_POST(self):
        """Handle POST /ask — send a question, get a structured answer."""
        if self.path != "/ask":
            self.send_response(404)
            self.end_headers()
            return
        
        # Read the request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        
        try:
            data = json.loads(body)
            question = data.get("question", "")
            
            if not question:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "question is required"}')
                return
            
            # Call our agent
            response = ask_agent(question)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.model_dump_json().encode())
            
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
    
    def do_GET(self):
        """Health check endpoint."""
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "healthy"}')
            return
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"message": "AI Agent API. POST /ask with {question: ...}"}')


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), AgentHandler)
    print(f"🚀 Agent server running on port {port}")
    server.serve_forever()

package main

import "net/http"

type Server struct{}

func (s *Server) Health(w http.ResponseWriter, r *http.Request) {}

func main() {
	s := &Server{}
	http.HandleFunc("GET /health", s.Health)
}

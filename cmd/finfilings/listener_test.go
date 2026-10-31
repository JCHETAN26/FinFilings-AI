package main

import (
	"errors"
	"net"
	"testing"
	"time"
)

func TestBoundedListenerCloseUnblocksAdmission(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	bounded := &boundedListener{Listener: listener, slots: make(chan struct{}, 1), done: make(chan struct{})}
	// Fill admission without inventing a network connection; Accept must wait.
	bounded.slots <- struct{}{}
	done := make(chan error, 1)
	go func() { _, e := bounded.Accept(); done <- e }()
	if err = bounded.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case err = <-done:
		if !errors.Is(err, net.ErrClosed) {
			t.Fatalf("wrong close error: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("listener deadlocked while full")
	}
	<-bounded.slots
}

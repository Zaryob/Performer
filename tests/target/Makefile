# Frame pointers are mandatory here: this binary doubles as the fixture that
# proves the preflight frame pointer check passes when it should.
CXX      ?= g++
CXXFLAGS ?= -O2 -g -fno-omit-frame-pointer -pthread -std=c++17 -Wall -Wextra
LDFLAGS  ?= -pthread

contention: contention.cc
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

.PHONY: clean
clean:
	rm -f contention

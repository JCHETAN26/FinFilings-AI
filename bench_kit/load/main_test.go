package main

import "testing"

func TestExactNearestRankQuantiles(t *testing.T) {
	values := make([]int64, 200)
	for i := range values {
		values[i] = int64(200 - i)
	}
	got := quantiles(values)
	if got != (percentiles{100, 190, 198, 200}) {
		t.Fatalf("wrong empirical quantiles: %+v", got)
	}
	one := quantiles([]int64{7})
	if one != (percentiles{7, 7, 7, 7}) {
		t.Fatal(one)
	}
}

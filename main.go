package main

import (
	"archive/zip"
	"bytes"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sort"
	"sync"
	"time"

	transit "github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"google.golang.org/protobuf/proto"
)

const (
	rtFeedURL    = "https://open-data.rtd-denver.com/files/gtfs-rt/rtd/TripUpdate.pb"
	gtfsZipURL   = "https://www.rtd-denver.com/files/gtfs/google_transit.zip"
	feedCacheTTL = 30 * time.Second
	gtfsCacheTTL = 24 * time.Hour
)

// staticData holds lookup tables derived from RTD's static GTFS schedule.
type staticData struct {
	stopNames map[string]string // stop_id → stop_name
	headsigns map[string]string // trip_id → trip_headsign
}

var (
	mu       sync.RWMutex
	rtFeed   *transit.FeedMessage
	rtFeedAt time.Time
	static   *staticData
	staticAt time.Time
)

// --- Static GTFS (refreshed daily) ---

func getStatic() *staticData {
	mu.RLock()
	if static != nil && time.Since(staticAt) < gtfsCacheTTL {
		s := static
		mu.RUnlock()
		return s
	}
	mu.RUnlock()

	mu.Lock()
	defer mu.Unlock()

	if static != nil && time.Since(staticAt) < gtfsCacheTTL {
		return static
	}

	s, err := fetchStatic()
	if err != nil {
		log.Printf("static GTFS fetch failed: %v", err)
		if static != nil {
			return static // serve stale on error
		}
		return &staticData{stopNames: map[string]string{}, headsigns: map[string]string{}}
	}

	static = s
	staticAt = time.Now()
	log.Printf("loaded %d stops, %d trips from static GTFS", len(s.stopNames), len(s.headsigns))
	return static
}

func fetchStatic() (*staticData, error) {
	resp, err := http.Get(gtfsZipURL)
	if err != nil {
		return nil, fmt.Errorf("fetch gtfs zip: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read gtfs zip: %w", err)
	}

	zr, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		return nil, fmt.Errorf("open gtfs zip: %w", err)
	}

	s := &staticData{
		stopNames: map[string]string{},
		headsigns: map[string]string{},
	}

	for _, f := range zr.File {
		switch f.Name {
		case "stops.txt":
			if err := parseCSV(f, func(row map[string]string) {
				if id := row["stop_id"]; id != "" {
					s.stopNames[id] = row["stop_name"]
				}
			}); err != nil {
				return nil, fmt.Errorf("parse stops.txt: %w", err)
			}
		case "trips.txt":
			if err := parseCSV(f, func(row map[string]string) {
				if id := row["trip_id"]; id != "" {
					s.headsigns[id] = row["trip_headsign"]
				}
			}); err != nil {
				return nil, fmt.Errorf("parse trips.txt: %w", err)
			}
		}
	}

	return s, nil
}

func parseCSV(f *zip.File, fn func(map[string]string)) error {
	rc, err := f.Open()
	if err != nil {
		return err
	}
	defer rc.Close()

	r := csv.NewReader(rc)
	headers, err := r.Read()
	if err != nil {
		return err
	}

	for {
		row, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		m := make(map[string]string, len(headers))
		for i, h := range headers {
			if i < len(row) {
				m[h] = row[i]
			}
		}
		fn(m)
	}
	return nil
}

// --- RT feed (refreshed every 30s) ---

func getRTFeed() (*transit.FeedMessage, error) {
	mu.RLock()
	if rtFeed != nil && time.Since(rtFeedAt) < feedCacheTTL {
		f := rtFeed
		mu.RUnlock()
		return f, nil
	}
	mu.RUnlock()

	mu.Lock()
	defer mu.Unlock()

	if rtFeed != nil && time.Since(rtFeedAt) < feedCacheTTL {
		return rtFeed, nil
	}

	resp, err := http.Get(rtFeedURL)
	if err != nil {
		return nil, fmt.Errorf("fetch rt feed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read rt feed: %w", err)
	}

	f := &transit.FeedMessage{}
	if err := proto.Unmarshal(body, f); err != nil {
		return nil, fmt.Errorf("unmarshal rt feed: %w", err)
	}

	rtFeed = f
	rtFeedAt = time.Now()
	return rtFeed, nil
}

// --- HTTP handlers ---

type Prediction struct {
	Route    string `json:"route"`
	Headsign string `json:"headsign"`
	Minutes  int    `json:"minutes"`
}

type PredictionsResponse struct {
	StopID      string       `json:"stop_id"`
	StopName    string       `json:"stop_name"`
	Predictions []Prediction `json:"predictions"`
	Count       int          `json:"count"`
	Timestamp   int64        `json:"timestamp"`
}

func handlePredictions(w http.ResponseWriter, r *http.Request) {
	stopID := r.PathValue("stop_id")

	feed, err := getRTFeed()
	if err != nil {
		log.Printf("rt feed error: %v", err)
		jsonError(w, http.StatusBadGateway, "upstream unavailable")
		return
	}

	s := getStatic()
	now := time.Now()
	var predictions []Prediction

	for _, entity := range feed.Entity {
		tu := entity.TripUpdate
		if tu == nil || tu.Trip == nil {
			continue
		}

		tripID := ""
		if tu.Trip.TripId != nil {
			tripID = *tu.Trip.TripId
		}
		routeID := ""
		if tu.Trip.RouteId != nil {
			routeID = *tu.Trip.RouteId
		}

		for _, stu := range tu.StopTimeUpdate {
			if stu.StopId == nil || *stu.StopId != stopID {
				continue
			}

			var arrivalUnix int64
			if stu.Arrival != nil && stu.Arrival.Time != nil {
				arrivalUnix = *stu.Arrival.Time
			} else if stu.Departure != nil && stu.Departure.Time != nil {
				arrivalUnix = *stu.Departure.Time
			} else {
				continue
			}

			mins := int(time.Unix(arrivalUnix, 0).Sub(now).Minutes())
			if mins < 0 {
				continue
			}

			predictions = append(predictions, Prediction{
				Route:    routeID,
				Headsign: s.headsigns[tripID],
				Minutes:  mins,
			})
			break // one prediction per trip
		}
	}

	sort.Slice(predictions, func(i, j int) bool {
		return predictions[i].Minutes < predictions[j].Minutes
	})

	if predictions == nil {
		predictions = []Prediction{}
	}

	writeJSON(w, http.StatusOK, PredictionsResponse{
		StopID:      stopID,
		StopName:    s.stopNames[stopID],
		Predictions: predictions,
		Count:       len(predictions),
		Timestamp:   now.Unix(),
	})
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":    "healthy",
		"timestamp": time.Now().Unix(),
	})
}

func handleRoot(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"cache_ttl":   30,
		"data_source": "RTD Denver GTFS-Realtime",
		"example":     "/predictions/11981",
		"health":      "/health",
		"service":     "RTD GTFS-RT Proxy for Tidbyt",
		"usage":       "GET /predictions/<stop_id>",
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func jsonError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	// Pre-warm static GTFS so first request doesn't block
	go getStatic()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", handleRoot)
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /predictions/{stop_id}", handlePredictions)

	log.Printf("listening on :%s", port)
	if err := http.ListenAndServe(":"+port, mux); err != nil {
		log.Fatal(err)
	}
}

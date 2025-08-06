import { Box, Button, Checkbox, Chip, Flex, Group, Input, Popover, Modal, Select, Slider, SliderProps, Stack, Text, Container, Title, Divider, Space } from '@mantine/core';
import { IconChevronDown, IconCurrentLocation, IconMapPlus, IconX } from '@tabler/icons-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import geocoding from '@mapbox/mapbox-sdk/services/geocoding';
import Map, { MapRef, LngLatLike, Source, Layer, Popup } from 'react-map-gl';
import { debounce } from 'lodash';
import { useRouter } from 'next/router';

import { COLORS, MAPBOX_TOKEN } from '../utils/constants';
import { getSite, getSiteFeatureSupport, getSites, getUSGSSites, registerSite } from '../utils/api';
import { Feature, Geometry } from '@mapbox/mapbox-sdk/services/geocoding-v6';
import { getUsgsSiteUrl } from '../utils/misc';
import { Site, SiteFeatureSupport } from '../utils/types';

const SELECTOR_LABEL_PROPS = { size: 'xs', fw: 500 };

export const PRESET_TIMEFRAMES: { [label: string]: number } = {
  '10 days': 10 * 24,
  '14 days': 14 * 24,
  '1 month': 30 * 24,
  '3 months': 3 * 30 * 24
};

export const PRESET_ACCURACY_HORIZONS = {
  '6h': 6 * 60 * 60,
  '12h': 12 * 60 * 60,
  '1d': 24 * 60 * 60,
  '2d': 2 * 24 * 60 * 60
};
export const DEFAULT_ACCURACY_HORIZON_IDX = 2;
const PRESET_ACCURACY_HORIZON_MARKS = Object.keys(PRESET_ACCURACY_HORIZONS)
  .reduce((marks: NonNullable<SliderProps['marks']>, label, idx) => {
    marks.push({ label, value: idx * 100 / (Object.keys(PRESET_ACCURACY_HORIZONS).length - 1) });
    return marks;
  }, []);

type SelectorProps = {
  features: string[],
  setFeatures: (features: string[]) => void,
  timeframe: string,
  setTimeframe: (timeframe: string) => void,
  showHistoricalAccuracy: boolean,
  setShowHistoricalAccuracy: (show: boolean) => void,
  historicalAccuracyHorizon: number,
  setHistoricalAccuracyHorizon: (horizon: number) => void
};

const extractSiteName = (description: string): string | null => {
  const parser = new DOMParser();
  const doc = parser.parseFromString(description, 'text/html');
  const siteNameElement = Array.from(doc.querySelectorAll('td')).find(td =>
    td.innerHTML.includes('Site Name:')
  );
  if (siteNameElement) {
    return siteNameElement.innerHTML.replace('Site Name:', '').replace('<b></b>', '').trim();
  }
  return null;
};

const Selector = (props: SelectorProps) => {
  const router = useRouter();
  const [mapOpened, setMapOpened] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [viewport, setViewport] = useState({
    latitude: 40.7128,
    longitude: -74.0060,
    zoom: 12
  });
  const [flowcastSites, setFlowcastSites] = useState<Site[]>([]);
  const [usgsSites, setUsgsSites] = useState<any>(false);
  const [hoveredFeature, setHoveredFeature] = useState<Feature | null>();
  const [selectedFeature, setSelectedFeature] = useState<Feature | null>();
  const [selectedFeatureSiteInfo, setSelectedFeatureSiteInfo] = useState<Site | null>();
  const [selectedFeatureSupportedMeasurements, setSelectedFeatureSupportedMeasurements] = useState<SiteFeatureSupport | null>();
  const mapRef = useRef<MapRef>(null);
  const geocodingClient = geocoding({ accessToken: MAPBOX_TOKEN });

  const { site } = router.query as { site: string };

  useEffect(() => {
    if (!!selectedFeature?.properties.name) {
      setSelectedFeatureSiteInfo(null);
      setSelectedFeatureSupportedMeasurements(null);

      getSite(selectedFeature.properties.name)
        .then((info) => {
          setSelectedFeatureSiteInfo(info);
          console.log(info);
          getSiteFeatureSupport(selectedFeature.properties.name)
            .then((support) => {
              setSelectedFeatureSupportedMeasurements(support);
              console.log(support);
            })
            .catch(() => setSelectedFeatureSupportedMeasurements(null));
        })
        .catch(() => setSelectedFeatureSiteInfo(null));
    }
  }, [selectedFeature]);

  useEffect(() => {
    getSites()
      .then(setFlowcastSites);
  }, []);

  const handleIconClick = (event: { stopPropagation: () => void; }) => {
    event.stopPropagation();
    setMapOpened(true);
  };

  const handleSearch = async () => {
    const response = await geocodingClient.forwardGeocode({
      query: searchQuery,
      limit: 5
    }).send();
    const [topResult] = response.body.features;
    if (!topResult) return;
    setViewport({
      latitude: topResult.center[1],
      longitude: topResult.center[0],
      zoom: 12
    });
    if (!mapRef.current) return;
    mapRef.current.flyTo({
      center: topResult.center as LngLatLike,
      zoom: 12
    });
  };

  const handleSliderChange = (val: number) => {
    const label = PRESET_ACCURACY_HORIZON_MARKS.find(({ value }) => value === val)?.label;
    if (label) {
      const horizon = PRESET_ACCURACY_HORIZONS[label as keyof typeof PRESET_ACCURACY_HORIZONS];
      if (horizon) props.setHistoricalAccuracyHorizon(horizon);
    }
  };

  const requestCurrentLocation = () => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (position) => {
          const { latitude, longitude } = position.coords;
          setViewport({
            latitude,
            longitude,
            zoom: 12
          });
          if (!mapRef.current) return;
          mapRef.current.flyTo({
            center: [longitude, latitude],
            zoom: 12
          });
        },
        (error) => {
          console.error("Error getting current location:", error);
        }
      );
    } else {
      console.error("Geolocation is not supported by this browser.");
    }
  };

  const updateSiteCandidates = useRef(debounce(async () => {
    const bounds = mapRef.current?.getBounds();
    if (!bounds) return;
    const bbox = [
      bounds.getWest(),
      bounds.getSouth(),
      bounds.getEast(),
      bounds.getNorth()
    ];
    const sites = await getUSGSSites(bbox)
    setUsgsSites(sites);
  }, 500));

  const handleHover = useCallback((event: any) => {
    const feature = event.features && event.features[0];
    if (feature) {
      if (feature !== hoveredFeature) setHoveredFeature(feature);
      if (!!mapRef.current) mapRef.current.getCanvas().style.cursor = 'pointer';
    } else {
      setHoveredFeature(null);
      if (!!mapRef.current) mapRef.current.getCanvas().style.cursor = '';
    }
  }, [hoveredFeature]);

  const handleClick = useCallback((event: any) => {
    const feature = event.features && event.features[0];
    if (feature) {
      if (!!mapRef.current) mapRef.current.flyTo({
        center: feature.geometry.center
      });
      setSelectedFeature(feature);
    } else {
      setSelectedFeature(null);
    }
  }, []);

  const handleOnboard = async () => {
    const site = selectedFeature?.properties.name;
    if (!site) return;

    if (!!(await registerSite(site))) {
      router.push(`/sites/${site}/onboard`)
    }
  };

  const selectSite = (name: string | null) => {
    const selectedSite = flowcastSites.find(s => s.name === name)?.usgs_site;
    if (!selectedSite) return null;
    router.push(`/sites/${selectedSite}`)
  }

  return (
    <Flex style={{ width: '100%' }}>
      <Stack gap={5}>
        <Text {...SELECTOR_LABEL_PROPS}>Location</Text>
        <Select
          placeholder='enter a location'
          data={flowcastSites.map(s => s.name)}
          value={flowcastSites.find(s => s.usgs_site === site)?.name}
          searchable
          styles={{
            input: {
              color: 'black'
            }
          }}
          rightSection={(
            <Button
              variant='transparent'
              style={{ padding: '0px 5px', cursor: 'pointer', pointerEvents: 'auto' }}
              onClick={handleIconClick}
            >
              <IconMapPlus size={18} />
            </Button>
          )}
          onChange={selectSite}
        />
      </Stack>
      <Stack gap={5} style={{ marginLeft: 20 }}>
        <Text {...SELECTOR_LABEL_PROPS}>Features</Text>
        <Chip.Group
          multiple
          value={props.features}
          onChange={props.setFeatures}
        >
          <Group
            style={{
              marginTop: '3px'
            }}
            gap={10}
          >
            <Chip value='watertemp' color={COLORS.CARROT_ORANGE}>Water Temperature</Chip>
            <Chip value='streamflow' color={COLORS.VISTA_BLUE}>Stream Flow</Chip>
          </Group>
        </Chip.Group>
      </Stack>
      <Box style={{ flexGrow: 1 }} />
      <Stack align='end' justify='end' gap={5}>
        <Popover position="bottom-end">
          <Popover.Target>
            <Button variant='light' style={{ paddingLeft: 5, paddingRight: 5 }}>
              <IconChevronDown size={25} />
            </Button>
          </Popover.Target>
          <Popover.Dropdown>
            <Stack>
              <Text {...SELECTOR_LABEL_PROPS}>Show Historical Accuracy</Text>
              <Flex align='center' style={{ marginBottom: 10 }}>
                <Checkbox
                  size='md'
                  style={{ marginRight: 10 }}
                  onChange={(event) => props.setShowHistoricalAccuracy(event.target.checked)}
                  defaultChecked={props.showHistoricalAccuracy}
                />
                <Slider
                  style={{ width: 150 }}
                  marks={PRESET_ACCURACY_HORIZON_MARKS}
                  step={100 / (Object.keys(PRESET_ACCURACY_HORIZON_MARKS).length - 1)}
                  defaultValue={(Object.values(PRESET_ACCURACY_HORIZONS)
                    .findIndex(v => v === props.historicalAccuracyHorizon) / (Object.keys(PRESET_ACCURACY_HORIZONS).length - 1)) * 100}
                  onChange={handleSliderChange}
                  label={null}
                />
              </Flex>
            </Stack>
          </Popover.Dropdown>
        </Popover>
      </Stack>
      <Stack align='end' gap={5} style={{ marginLeft: 10 }}>
        <Text {...SELECTOR_LABEL_PROPS}>Timeframe</Text>
        <Flex align='center' gap='sm'>
          <Select
            data={[...Object.keys(PRESET_TIMEFRAMES)]}
            value={props.timeframe}
            onChange={value => !!value && props.setTimeframe(value)}
            style={{
              width: 110
            }}
          />
        </Flex>
      </Stack>

      <Modal
        opened={mapOpened}
        onClose={() => setMapOpened(false)}
        centered
        size='75%'
        withCloseButton={false}
        styles={{
          content: {
            borderRadius: 10
          },
          body: {
            padding: 0
          }
        }}
      >
        <Flex>
          <Container
            fluid
            style={{
              flexGrow: 1,
              padding: 0,
              position: 'relative'
            }}
          >
            <Flex
              style={{
                width: 'calc(100% - 20px)',
                boxSizing: 'border-box',
                position: 'absolute',
                top: '10px',
                left: '10px',
                zIndex: 1
              }}
              gap={10}
              align='center'
            >
              <Input
                placeholder='Search location'
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                style={{ flex: 1 }}
              />
              <Button
                onClick={requestCurrentLocation}
                variant={'light'}
                size='compact-lg'
              >
                <IconCurrentLocation size={20} color={COLORS.DAVY_GRAY} />
              </Button>
              <Button
                onClick={() => setMapOpened(false)}
                variant={'light'}
                size='compact-lg'
              >
                <IconX size={20} color={COLORS.DAVY_GRAY} />
              </Button>
            </Flex>
            <Map
              ref={mapRef}
              style={{ height: '75vh', width: '100%' }}
              initialViewState={viewport}
              mapStyle='mapbox://styles/jaismith/clvystdxm07i701phgm3g0frm'
              mapboxAccessToken={MAPBOX_TOKEN}
              onLoad={updateSiteCandidates.current}
              onMoveEnd={updateSiteCandidates.current}
              onMouseMove={handleHover}
              onClick={handleClick}
              interactiveLayerIds={['data']}
            >
              {hoveredFeature && (
                <Popup
                  longitude={(hoveredFeature.geometry as any).coordinates[0]}
                  latitude={(hoveredFeature.geometry as any).coordinates[1]}
                  closeButton={false}
                  closeOnClick={false}
                  offset={5}
                >
                  <div>{extractSiteName((hoveredFeature.properties as unknown as { description: string }).description)}</div>
                </Popup>
              )}
              {usgsSites && (
                <Source type="geojson" data={usgsSites}>
                  <Layer
                    id="data"
                    type="circle"
                    paint={{
                      'circle-radius': 5,
                      'circle-color': COLORS.VISTA_BLUE,
                      'circle-stroke-width': 5,
                      'circle-stroke-color': 'transparent'
                    }}
                  />
                </Source>
              )}
              {selectedFeature && (
                <Source type="geojson" data={selectedFeature as GeoJSON.Feature}>
                  <Layer
                    id="selected-feature"
                    type="circle"
                    paint={{
                      'circle-radius': 5,
                      'circle-color': COLORS.CARROT_ORANGE,
                      'circle-stroke-width': 5,
                      'circle-stroke-color': 'transparent'
                    }}
                  />
                </Source>
              )}
            </Map>
          </Container>
          <Container
            style={{
              borderRadius: '10px',
              boxSizing: 'border-box',
              padding: !!selectedFeature ? 20 : 0,
              width: !!selectedFeature ? '35%' : 0,
              transition: 'width 0.15s',
              transitionTimingFunction: 'cubic-bezier(0.4, 0, 0.2, 1)'
            }}
          >
            {!!selectedFeature && (
              <>
                <Title fw='lighter'>{extractSiteName((selectedFeature.properties as unknown as { description: string }).description)}</Title>
                <Space style={{ height: 5 }} />
                <Divider />
                <Space style={{ height: 5 }} />
                <Text><a href={getUsgsSiteUrl(selectedFeature.properties.name)}>USGS Site: {selectedFeature.properties.name}</a></Text>
                <Text>Status: {!!selectedFeatureSiteInfo ? selectedFeatureSiteInfo.status : 'Not Onboarded'}</Text>
                <Space style={{ height: 5 }} />
                <Divider />
                <Space style={{ height: 5 }} />
                <Text>Collects Stream Flow Measurements: {selectedFeatureSupportedMeasurements?.hasStreamFlow ? 'YES' : 'NO'}</Text>
                <Text>Collects Water Temperature Measurements: {selectedFeatureSupportedMeasurements?.hasWaterTemp ? 'YES' : 'NO'}</Text>
                {(!selectedFeatureSiteInfo || selectedFeatureSiteInfo.status === 'FAILED') && selectedFeatureSupportedMeasurements?.hasStreamFlow && selectedFeatureSupportedMeasurements.hasWaterTemp && (
                  <Button
                    style={{
                      position: 'absolute',
                      width: 'calc(35% - 20px)',
                      height: 60,
                      bottom: 10,
                      right: 10
                    }}
                    variant='gradient'
                    onClick={handleOnboard}
                  >
                    Onboard
                  </Button>
                )}
              </>
            )}
          </Container>
        </Flex>
      </Modal>
    </Flex>
  );
}

export default Selector;

import {
  Box,
  Group,
  Stack,
  Title,
  Text,
  Space
} from '@mantine/core';
import Map from 'react-map-gl';
import { IconMapPinFilled } from '@tabler/icons-react';

import { getSite, getReport } from '../utils/api';

import type { Site } from '../utils/types';
import { useEffect, useState } from 'react';
import { COLORS } from '../utils/constants';

type SitePageProps = {
  usgs_site: string
};

const SiteDetail = ({ usgs_site }: SitePageProps) => {
  const [site, setSite] = useState<Site | null>();
  const [report, setReport] = useState<string | null>(null);

  useEffect(() => {
    (async function () {
      setSite(await getSite(usgs_site));
      setReport((await getReport(usgs_site)));
    })()
  }, [usgs_site]);

  useEffect(() => {
    (async function () {
      const { bouncy } = await import('ldrs')
      bouncy.register()
    })()
  }, []);

  return !!site ? (
    <Group justify='space-between' wrap='nowrap' gap="xl">
      <Stack style={{ marginBottom: 'auto' }} gap={2.5}>
        <Title fw='lighter'>{site?.name}</Title>
        <Text size='sm'><a href={`https://waterdata.usgs.gov/monitoring-location/${usgs_site}`}>USGS Site {site?.usgs_site}</a>, Latitude: {site?.latitude}, Longitude: {site?.longitude}</Text>
        <Space style={{ height: 15 }} />
        <Text ff='monospace'>
          {report}
        </Text>
      </Stack>
      <Box style={{ width: 400, height: 500, borderRadius: 10, overflow: 'hidden', minWidth: 400 }}>
        <Map
          mapboxAccessToken='pk.eyJ1IjoiamFpc21pdGgiLCJhIjoiY2s3OTd1eGZwMHA0ZDNuczcxOWhxN2FlciJ9.lDRwoL1DHGink9Hlv97vww'
          longitude={parseFloat(site.longitude)}
          latitude={parseFloat(site.latitude)}
          zoom={10}
          mapStyle='mapbox://styles/jaismith/clvystdxm07i701phgm3g0frm'
        />
        <IconMapPinFilled
          width={15}
          color={COLORS.DAVY_GRAY}
          style={{ transform: 'translate(-50%, -50%) translate(200px, -250px)' }}
        />
      </Box>
    </Group>
  ) : (
    <l-bouncy
      size="45"
      speed="1.75" 
      color="black" 
    ></l-bouncy>
  );
};

export default SiteDetail;

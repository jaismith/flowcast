import { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import { Text, Container, Stack, Space, Group, Flex, Title, Button, Divider, Image, Timeline, Box } from "@mantine/core";
import { IconBrain, IconCrystalBall, IconDeviceFloppy, IconHourglassHigh, IconInbox, IconCheck } from "@tabler/icons-react";
import ScrollToBottom from 'react-scroll-to-bottom';
import { css } from '@emotion/css';

import { Site, SiteFeatureSupport, SiteUpdate, SiteUpdateSchema } from "../../../utils/types";
import { getSite, getSiteFeatureSupport } from "../../../utils/api";
import { ACCESS_API_WSS, COLORS } from "../../../utils/constants";

type OnboardProps = {
  siteInfo: Site,
  siteFeatureSupport: SiteFeatureSupport
};

const LOG_CONTAINER_CSS = css({
  borderRadius: 10,
  backgroundColor: COLORS.MAGNOLIA,
  padding: '15px 20px'
});

const STATUS_TO_ACTIVE_ITEM: Record<string, number> = {
  'SCHEDULED': 0,
  'FETCHING_DATA': 1,
  'EXPORTING_SNAPSHOT': 2,
  'TRAINING_MODELS': 3,
  'FORECASTING': 4,
  'ACTIVE': 5
};

export const getServerSideProps = async (context: any) => {
  const { site } = context.params;

  if (!site || typeof site !== 'string') {
    return {
      notFound: true
    };
  }

  const siteInfo = await getSite(site);
  const siteFeatureSupport = await getSiteFeatureSupport(site);

  return {
    props: {
      siteInfo,
      siteFeatureSupport
    }
  };
}

const Onboard = ({ siteInfo, siteFeatureSupport }: OnboardProps) => {
  const router = useRouter();

  const [latestSiteUpdate, setSiteUpdate] = useState<SiteUpdate>({
    status: siteInfo.status,
    onboarding_logs: siteInfo.onboarding_logs ?? [],
    usgs_site: siteInfo.usgs_site
  });
  
  useEffect(() => {
    const socket = new WebSocket(ACCESS_API_WSS + `?usgs_site=${siteInfo.usgs_site}`);

    socket.onmessage = (ev: MessageEvent<string>) => {
      const { data } = ev;
      const siteUpdate = SiteUpdateSchema.parse(JSON.parse(data));
      setSiteUpdate(siteUpdate);

      if (siteUpdate.status === 'ACTIVE') {
        setTimeout(() => router.push(`/sites/${siteInfo.usgs_site}`), 1000)
      }
    };
  });

  return (
    <Container size='lg'>
      <Stack>
        <Space />
        <Group justify='space-between'>
          <Flex gap='sm'>
            <Image
              src="/static/logo.png"
              component="img"
              alt='decorative logo'
              style={{ width: 30 }}
              fit="contain"
            />
            <Title order={2} fw={500} style={{ color: 'rgb(32, 55, 67)' }}>flowcast</Title>
          </Flex>
          <Button
            variant='outline'
            component='a'
            href='https://github.com/jaismith/flowcast'
            target='_blank'
            rel='noopener noreferrer'
          >
            Github
          </Button>
        </Group>
        <Divider />
        <Space style={{ height: 20 }} />
        <Flex style={{ width: '80%', marginLeft: '10%' }}>
          <Timeline
            active={STATUS_TO_ACTIVE_ITEM[latestSiteUpdate.status]}
            bulletSize={30}
            style={{
              flex: 1,
              marginRight: 40
            }}
          >
            <Timeline.Item bullet={<IconHourglassHigh size={16} />} title='Scheduled'>
              <Text c='dimmed' size='sm'>Site onboarding will begin momentarily.</Text>
            </Timeline.Item>
            <Timeline.Item bullet={<IconInbox size={16} />} title='Fetching Data'>
              <Text c='dimmed' size='sm'>Historical water conditions and atmospheric weather are being fetched for {siteInfo.name}.</Text>
            </Timeline.Item>
            <Timeline.Item bullet={<IconDeviceFloppy size={16} />} title='Exporting Snapshot'>
              <Text c='dimmed' size='sm'>Exporting a snapshot of your data for model training.</Text>
            </Timeline.Item>
            <Timeline.Item bullet={<IconBrain size={16} />} title='Training Models'>
              <Text c='dimmed' size='sm'>Training models for each feature localized to {siteInfo.name}.</Text>
            </Timeline.Item>
            <Timeline.Item bullet={<IconCrystalBall size={16} />} title='Forecasting Conditions'>
              <Text c='dimmed' size='sm'>Forecasting conditions for the upcoming week.</Text>
            </Timeline.Item>
            <Timeline.Item bullet={<IconCheck size={16} />} color='green' title='Site Active'>
              <Text c='dimmed' size='sm'>Onboarding complete! Redirecting...</Text>
            </Timeline.Item>
          </Timeline>
          <Stack style={{ flex: 2 }}>
            <Box>
              <Title fw='lighter'>
                <span style={{ fontWeight: 400 }}>ONBOARDING: </span>{siteInfo.name}
              </Title>
            </Box>
            <ScrollToBottom className={LOG_CONTAINER_CSS}>
              {latestSiteUpdate.onboarding_logs.map((log, idx) => (
                <Text
                  key={idx}
                  size='sm'
                  ff='monospace'
                  style={{
                    whiteSpace: 'pre',
                    tabSize: 4,
                    margin: 2,
                    textWrap: 'wrap',
                    color: COLORS.DAVY_GRAY
                  }}
                >
                  {log}
                </Text>
              ))}
            </ScrollToBottom>
          </Stack>
        </Flex>
      </Stack>
    </Container>
  );
};

export default Onboard;

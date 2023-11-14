import { useState, useEffect } from 'react';
import styled from 'styled-components';

import type { Datapoint } from 'utils/types';
import { getForecast } from 'utils/api';
import { Chart } from 'chart/chart';
import { COLORS } from 'utils/constants';

const Container = styled.div`
  display: flex;
  flex-direction: column;
  align-items: stretch;
  max-width: 750px;
  margin: 50px auto;
  color: ${COLORS['drab-dark-brown']};
`;

const Navbar = styled.nav`
  display: inline-flex;
  justify-content: space-between;
  padding-bottom: 5px;
  margin: 10px 20px;
  font-size: 1.25em;
  font-style: italic;
  border-bottom: 1px solid;
`;

const CTA = styled.div`
  margin: 20px 20px 5%;
  font-style: italic;
  font-weight: light;
  font-size: 1em;
`;

const ChartContainer = styled.div`
  height: 400px;
`;

export const App = () => {
  const [data, setData] = useState<Datapoint[]>([]);

  useEffect(() => {
    getForecast().then(forecast => setData(forecast))
  }, [setData]);

  return (
    <Container className='App'>
      <Navbar>
        <a href="https://flowcast.jaismith.dev">flowcast</a>
        <a href="https://github.com/jaismith/flowcast">github</a>
      </Navbar>
      <CTA>
        Simple water temperature forecasting, built on 10+ years of USGS and NOAA weather data.
      </CTA>
      <ChartContainer>
        {data.length > 1 && <Chart data={data} />}
      </ChartContainer>
    </Container>
  );
}

export default App;
